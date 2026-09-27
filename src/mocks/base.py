"""Shared machinery for every mock service (MS-8, MS-9).

Each mock is a separate FastAPI app with:
- state loaded from seed files, reset on demand, optionally persisted to disk;
- idempotent action endpoints (same key + same payload -> original result; same key +
  different payload -> 409);
- a call log of every request it receives, for evals to assert what was and was not called;
- fault injection (error rate, latency, timeouts, scripted failures);
- admin endpoints under /_admin (never subject to faults, never logged as calls).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mocks.seed import load_seed_files
from router.config.models import ServiceFaults


class MockClock:
    """Real time unless pinned. Pinned time only moves when set via /_admin/now."""

    def __init__(self, fixed: datetime | None = None):
        self._fixed = fixed.astimezone(UTC) if fixed else None

    def now(self) -> datetime:
        return self._fixed or datetime.now(UTC).replace(microsecond=0)

    def set(self, at: datetime | None) -> None:
        if at is not None and at.tzinfo is None:
            raise ValueError("now must include a UTC offset")
        self._fixed = at.astimezone(UTC) if at else None

    @property
    def pinned(self) -> bool:
        return self._fixed is not None


def api_error(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "detail": detail})


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- faults


@dataclass
class Fault:
    kind: str  # error | timeout | timeout_after_commit
    source: str  # scripted | random


def _endpoint_regex(endpoint: str) -> tuple[str, re.Pattern[str]]:
    method, path = endpoint.split(" ", 1)
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
    return method.upper(), re.compile(f"^{pattern}$")


class FaultInjector:
    def __init__(self, enabled: bool, cfg: ServiceFaults | None, seed: int, hang_s: float):
        self.hang_s = hang_s
        self._seed = seed
        self.configure(enabled, cfg)

    def configure(self, enabled: bool, cfg: ServiceFaults | None) -> None:
        self.enabled = enabled
        self.cfg = cfg or ServiceFaults()
        self._scripted = [(_endpoint_regex(s.endpoint), s) for s in self.cfg.scripted]
        self.reset()

    def reset(self) -> None:
        self._rng = random.Random(self._seed)
        self._remaining = [s.fail_times for _, s in self._scripted]

    @property
    def latency_s(self) -> float:
        return self.cfg.latency_ms / 1000 if self.enabled else 0.0

    def decide(self, method: str, path: str, params: dict[str, Any]) -> Fault | None:
        if not self.enabled:
            return None
        for i, ((m, rx), rule) in enumerate(self._scripted):
            match = rx.match(path)
            if m != method or not match or self._remaining[i] <= 0:
                continue
            merged = {**params, **match.groupdict()}
            if all(str(merged.get(k)) == str(v) for k, v in rule.match.items()):
                self._remaining[i] -= 1
                return Fault(rule.kind, "scripted")
        roll = self._rng.random()
        if roll < self.cfg.error_rate:
            return Fault("error", "random")
        if roll < self.cfg.error_rate + self.cfg.timeout_rate:
            return Fault("timeout", "random")
        return None


# --------------------------------------------------------------------------- service


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    seed_files: tuple[str, ...]
    initial_data: Callable[[dict[str, Any]], dict[str, Any]]
    register: Callable[[FastAPI, MockService], None]
    on_reset: Callable[[MockService], None] | None = None


@dataclass
class MockService:
    spec: ServiceSpec
    seed_dir: Path
    clock: MockClock
    faults: FaultInjector
    state_dir: Path | None = None  # None = in-memory only (tests)
    extra: dict[str, Any] = field(default_factory=dict)  # service-specific settings (e.g. outbox dir)

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        self.calls: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}
        self.idem: dict[str, dict[str, Any]] = {}
        self.seq: dict[str, int] = {}
        self.seeded_now: str = ""
        self.seed_fingerprint: str = ""
        if not self._load_persisted():
            self.reset()

    @property
    def name(self) -> str:
        return self.spec.name

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        """Back to seed state, relative to the current clock; clears calls and idempotency keys."""
        with self.lock:
            now = self.clock.now()
            seed, fp = load_seed_files(self.seed_dir, self.spec.seed_files, now)
            self.data = self.spec.initial_data(seed)
            self.idem, self.seq, self.calls = {}, {}, []
            self.seeded_now, self.seed_fingerprint = now.isoformat(timespec="seconds"), fp
            self.faults.reset()
            if self.spec.on_reset:
                self.spec.on_reset(self)
            if self.state_dir:
                self._calls_file.unlink(missing_ok=True)
            self.save()

    def next_id(self, prefix: str, width: int = 6) -> str:
        with self.lock:
            self.seq[prefix] = self.seq.get(prefix, 0) + 1
            return f"{prefix}-{self.seq[prefix]:0{width}d}"

    @property
    def _state_file(self) -> Path:
        assert self.state_dir
        return self.state_dir / f"{self.name}.state.json"

    @property
    def _calls_file(self) -> Path:
        assert self.state_dir
        return self.state_dir / f"{self.name}.calls.jsonl"

    def save(self) -> None:
        if not self.state_dir:
            return
        with self.lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "seed_fingerprint": self.seed_fingerprint, "seeded_now": self.seeded_now,
                "data": self.data, "idem": self.idem, "seq": self.seq,
            }))
            tmp.replace(self._state_file)

    def _load_persisted(self) -> bool:
        """Resume persisted state only if it came from the same seed (and same pinned now)."""
        if not self.state_dir or not self._state_file.is_file():
            return False
        saved = json.loads(self._state_file.read_text())
        _, fp = load_seed_files(self.seed_dir, self.spec.seed_files, self.clock.now())
        if saved["seed_fingerprint"] != fp:
            return False
        if self.clock.pinned and saved["seeded_now"] != self.clock.now().isoformat(timespec="seconds"):
            return False
        self.data, self.idem, self.seq = saved["data"], saved["idem"], saved["seq"]
        self.seeded_now, self.seed_fingerprint = saved["seeded_now"], fp
        if self._calls_file.is_file():
            self.calls = [json.loads(line) for line in self._calls_file.read_text().splitlines() if line]
        return True

    # ------------------------------------------------------------------ calls / idempotency

    def record_call(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entry = {"seq": len(self.calls) + 1, **entry}
            self.calls.append(entry)
            if self.state_dir:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with self._calls_file.open("a") as f:
                    f.write(json.dumps(entry) + "\n")

    def idempotent(self, key: str | None, payload: dict[str, Any], create: Callable[[], dict[str, Any]]) -> JSONResponse:
        """Run `create` once per idempotency key (MS-3, MS-4, MS-6, FR-24)."""
        if not key:
            raise api_error(400, "idempotency_key_required", "Idempotency-Key header is required")
        digest = payload_hash(payload)
        with self.lock:
            if key in self.idem:
                rec = self.idem[key]
                if rec["hash"] != digest:
                    raise api_error(409, "idempotency_key_reused",
                                    "this Idempotency-Key was already used with a different payload")
                return JSONResponse(rec["body"], status_code=200, headers={"Idempotent-Replayed": "true"})
            body = create()  # validation errors raise and are NOT remembered, so a fixed retry can succeed
            self.idem[key] = {"hash": digest, "body": body}
            self.save()
            return JSONResponse(body, status_code=201, headers={"Idempotent-Replayed": "false"})


# --------------------------------------------------------------------------- app


def create_app(svc: MockService) -> FastAPI:
    app = FastAPI(title=f"mock-{svc.name}", version="1.0")

    @app.middleware("http")
    async def faults_and_call_log(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path.startswith("/_admin"):
            return await call_next(request)

        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw.decode(errors="replace")
        params: dict[str, Any] = {**request.query_params, **(body if isinstance(body, dict) else {})}
        fault = svc.faults.decide(request.method, path, params)
        started = time.perf_counter()

        if svc.faults.latency_s:
            await asyncio.sleep(svc.faults.latency_s)
        if fault and fault.kind == "error":
            response: Response = JSONResponse({"error": "injected_fault", "detail": "service unavailable"}, 503)
            resp_body: Any = None
        elif fault and fault.kind == "timeout":
            await asyncio.sleep(svc.faults.hang_s)
            response, resp_body = JSONResponse({"error": "injected_timeout"}, 504), None
        else:
            inner = await call_next(request)
            content = b"".join([chunk async for chunk in inner.body_iterator])
            response = Response(content, inner.status_code, dict(inner.headers), inner.media_type)
            try:
                resp_body = json.loads(content) if content else None
            except json.JSONDecodeError:
                resp_body = None
            if fault and fault.kind == "timeout_after_commit":
                await asyncio.sleep(svc.faults.hang_s)  # the action happened; the caller just never hears

        svc.record_call({
            "at": svc.clock.now().isoformat(timespec="seconds"),
            "method": request.method, "path": path, "query": dict(request.query_params),
            "body": body, "idempotency_key": request.headers.get("idempotency-key"),
            "status": response.status_code, "response": resp_body,
            "fault": fault.kind if fault else None, "fault_source": fault.source if fault else None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return response

    @app.get("/_admin/health")
    def health() -> dict[str, Any]:
        return {"service": svc.name, "now": svc.clock.now().isoformat(timespec="seconds"),
                "clock_pinned": svc.clock.pinned, "seeded_now": svc.seeded_now,
                "seed_fingerprint": svc.seed_fingerprint, "calls": len(svc.calls),
                "faults_enabled": svc.faults.enabled}

    @app.post("/_admin/reset")
    def reset(body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        if body and "now" in body:
            svc.clock.set(_parse_now(body["now"]))
        svc.reset()
        return health()

    @app.put("/_admin/now")
    def set_now(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Move the clock without reseeding (simulates time passing). null = real time."""
        svc.clock.set(_parse_now(body.get("now")))
        return health()

    @app.get("/_admin/calls")
    def calls(since: int = 0) -> list[dict[str, Any]]:
        with svc.lock:
            return [c for c in svc.calls if c["seq"] > since]

    @app.get("/_admin/faults")
    def get_faults() -> dict[str, Any]:
        return {"enabled": svc.faults.enabled, **svc.faults.cfg.model_dump()}

    @app.put("/_admin/faults")
    def put_faults(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        enabled = bool(body.pop("enabled", True))
        try:
            cfg = ServiceFaults.model_validate(body)
        except ValueError as exc:
            raise api_error(422, "invalid_faults", str(exc)) from None
        svc.faults.configure(enabled, cfg)
        return get_faults()

    svc.spec.register(app, svc)
    return app


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise api_error(422, "invalid_now", f"not an ISO-8601 datetime: {value!r}") from None
    if dt.tzinfo is None:
        raise api_error(422, "invalid_now", "now must include a UTC offset")
    return dt
