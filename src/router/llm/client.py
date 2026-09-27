"""LLM client: prompt -> provider -> strict JSON -> validated Pydantic object, or LLMFailure (LL-1..LL-5).

- Output that is not valid JSON, fails the schema, or fails extra validation is retried up to the
  step's `retries`; after that the caller gets LLMFailure and routes the case to a human (LL-3).
- Every call is reported to `on_call` with model, prompt version, input, output, tokens, latency
  and estimated cost (FR-39, LL-5).
- Recording modes: off | record | replay (EV-1). Replay never calls a model; record reuses any recorded
  output for the same input and calls the model only for new inputs (so reruns are incremental).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from router.config import LoadedConfig
from router.llm.prompts import load_prompt
from router.llm.providers import Provider, ProviderError, make_provider
from router.llm.replay import Recordings
from router.llm.schema import strict_schema

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class LLMCall:
    step: str
    provider: str
    model: str
    prompt_version: str
    prompt_digest: str
    system: str
    user: str
    output: str | None
    ok: bool
    error: str | None
    attempts: int
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    replayed: bool
    attempt_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    parsed: T
    call: LLMCall


class LLMFailure(Exception):
    def __init__(self, message: str, call: LLMCall):
        super().__init__(message)
        self.call = call


class LLMClient:
    def __init__(self, loaded: LoadedConfig, *, on_call: Callable[[LLMCall], None] | None = None,
                 providers: dict[str, Provider] | None = None, recording_mode: str | None = None,
                 recording_name: str | None = None):
        self.cfg = loaded.settings
        self.on_call = on_call
        self._providers = dict(providers or {})
        rec = self.cfg.llm.recording
        self.mode = recording_mode or rec.mode
        name = recording_name or rec.name
        self.recordings = Recordings(self.cfg.paths.recordings / f"{name}.jsonl") if self.mode != "off" else None

    def provider(self, name: str) -> Provider:
        if name not in self._providers:
            self._providers[name] = make_provider(self.cfg.llm.providers[name])
        return self._providers[name]

    def run(self, step: str, output_model: type[T], variables: dict[str, Any], *,
            enums: dict[str, list[str]] | None = None,
            validate: Callable[[T], list[str]] | None = None, prompt_name: str | None = None) -> LLMResult[T]:
        """`prompt_name` lets one step (model config) use another prompt folder, e.g. judge -> judge_summary."""
        sc = self.cfg.llm.steps[step]
        prompt = load_prompt(self.cfg.paths.prompts, prompt_name or step, sc.prompt_version)
        system, user = prompt.render(**variables)
        schema = strict_schema(output_model, enums)
        key = hashlib.sha256(json.dumps([sc.provider, sc.model, prompt.digest, system, user, schema],
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        started = time.perf_counter()
        base = dict(step=step, provider=sc.provider, model=sc.model, prompt_version=sc.prompt_version,
                    prompt_digest=prompt.digest, system=system, user=user)

        rec = self.recordings.get(key) if self.recordings and self.mode in {"replay", "record"} else None
        if self.mode == "replay" or rec is not None:  # record mode reads through: reruns only call for what's missing
            if rec is None:
                call = LLMCall(**base, output=None, ok=False, error="no recording for this input", attempts=0,
                               input_tokens=0, output_tokens=0, latency_ms=0.0, cost_usd=0.0, replayed=True)
                return self._fail(call)
            parsed, err = _parse(rec["output"], output_model, validate)
            call = LLMCall(**base, output=rec["output"], ok=err is None, error=err, attempts=0,
                           input_tokens=rec["input_tokens"], output_tokens=rec["output_tokens"],
                           latency_ms=rec["latency_ms"], cost_usd=self._cost(sc.model, rec["input_tokens"],
                                                                             rec["output_tokens"]), replayed=True)
            return self._done(parsed, call)

        errors: list[str] = []
        tokens_in = tokens_out = 0
        text: str | None = None
        attempt_user = user
        for attempt in range(1, sc.retries + 2):
            try:
                comp = self.provider(sc.provider).complete(sc, system, attempt_user, schema, step)
            except ProviderError as exc:
                errors.append(str(exc))
                if not exc.retryable:
                    break
                continue
            tokens_in += comp.input_tokens
            tokens_out += comp.output_tokens
            text = comp.text
            parsed, err = _parse(text, output_model, validate)
            if err is None:
                latency = round((time.perf_counter() - started) * 1000, 1)
                call = LLMCall(**base, output=text, ok=True, error=None, attempts=attempt, input_tokens=tokens_in,
                               output_tokens=tokens_out, latency_ms=latency,
                               cost_usd=self._cost(sc.model, tokens_in, tokens_out), replayed=False,
                               attempt_errors=errors)
                if self.mode == "record" and self.recordings is not None:
                    self.recordings.put({"key": key, "step": step, "model": sc.model,
                                         "prompt_version": sc.prompt_version, "output": text,
                                         "input_tokens": tokens_in, "output_tokens": tokens_out, "latency_ms": latency})
                return self._done(parsed, call)
            errors.append(err)
            # Tell the model what was wrong; the answer must still come from the same email.
            attempt_user = f"{user}\n\nYour previous answer was rejected: {err[:500]}\nReturn corrected JSON only."
        call = LLMCall(**base, output=text, ok=False, error=errors[-1] if errors else "unknown", attempts=len(errors),
                       input_tokens=tokens_in, output_tokens=tokens_out,
                       latency_ms=round((time.perf_counter() - started) * 1000, 1),
                       cost_usd=self._cost(sc.model, tokens_in, tokens_out), replayed=False, attempt_errors=errors)
        return self._fail(call)

    # ------------------------------------------------------------------ helpers

    def _cost(self, model: str, tin: int, tout: int) -> float:
        p = self.cfg.llm.pricing.get(model)
        return round((tin * p.input_per_mtok + tout * p.output_per_mtok) / 1e6, 6) if p else 0.0

    def _done(self, parsed: Any, call: LLMCall) -> LLMResult:
        if self.on_call:
            self.on_call(call)
        if parsed is None:
            raise LLMFailure(call.error or "invalid output", call)
        return LLMResult(parsed, call)

    def _fail(self, call: LLMCall) -> LLMResult:
        if self.on_call:
            self.on_call(call)
        raise LLMFailure(call.error or "LLM call failed", call)


def _parse(text: str, model: type[T], validate: Callable[[T], list[str]] | None) -> tuple[T | None, str | None]:
    try:
        parsed = model.model_validate_json(text)
    except ValidationError as exc:
        return None, f"schema validation failed: {exc.errors()[:3]}"
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"
    problems = validate(parsed) if validate else []
    return (parsed, None) if not problems else (None, "; ".join(problems))
