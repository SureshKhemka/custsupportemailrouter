"""HTTP access to backend services (section 4): timeouts, retries, error mapping, call recording.

Every call is reported to `on_call`, so the audit log sees every backend request and result
(FR-39). Retries are safe: reads are idempotent and every action carries an idempotency key.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from router.config.models import ServiceConfig

ErrorKind = Literal["timeout", "unavailable", "not_found", "rejected", "conflict", "bad_response"]


class ServiceError(Exception):
    def __init__(self, service: str, op: str, kind: ErrorKind, status: int | None = None, detail: Any = None):
        self.service, self.op, self.kind, self.status, self.detail = service, op, kind, status, detail
        super().__init__(f"{service} {op}: {kind}" + (f" ({status})" if status else "") + (f": {detail}" if detail else ""))

    @property
    def retryable(self) -> bool:
        return self.kind in {"timeout", "unavailable"}


@dataclass(frozen=True)
class CallRecord:
    service: str
    method: str
    path: str
    params: dict[str, Any] | None
    body: Any
    idempotency_key: str | None
    status: int | None
    response: Any
    error: str | None
    attempts: int
    latency_ms: float


class ServiceClient:
    def __init__(self, name: str, cfg: ServiceConfig, *, client: httpx.Client | None = None,
                 on_call: Callable[[CallRecord], None] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.name, self.cfg = name, cfg
        self._http = client or httpx.Client(base_url=cfg.base_url, timeout=cfg.timeout_s)
        self._on_call = on_call
        self._sleep = sleep

    def request(self, method: str, path: str, *, op: str, params: dict[str, Any] | None = None, body: Any = None,
                idempotency_key: str | None = None, allow_404: bool = False) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        attempts, started = 0, time.perf_counter()
        last: ServiceError | None = None
        status, payload = None, None
        while attempts <= self.cfg.retries:
            attempts += 1
            try:
                resp = self._http.request(method, path, params=params, json=body, headers=headers)
                status = resp.status_code
                payload = _json(resp)
                if status == 404 and allow_404:
                    self._record(method, path, params, body, idempotency_key, status, payload, None, attempts, started)
                    return None
                if status < 400:
                    self._record(method, path, params, body, idempotency_key, status, payload, None, attempts, started)
                    return payload
                last = ServiceError(self.name, op, _kind(status), status, _detail(payload))
            except httpx.TimeoutException:
                last = ServiceError(self.name, op, "timeout")
            except httpx.TransportError as exc:
                last = ServiceError(self.name, op, "unavailable", detail=type(exc).__name__)
            if not last.retryable or attempts > self.cfg.retries:
                break
            self._sleep(self.cfg.backoff_s * attempts)
        assert last is not None
        self._record(method, path, params, body, idempotency_key, status, payload, str(last), attempts, started)
        raise last

    def _record(self, method, path, params, body, key, status, payload, error, attempts, started) -> None:
        if self._on_call:
            self._on_call(CallRecord(self.name, method, path, params, body, key, status, payload, error, attempts,
                                     round((time.perf_counter() - started) * 1000, 1)))


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json() if resp.content else None
    except ValueError:
        return resp.text


def _kind(status: int) -> ErrorKind:
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in (408, 504):
        return "timeout"
    if status >= 500:
        return "unavailable"
    return "rejected"


def _detail(payload: Any) -> Any:
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload
