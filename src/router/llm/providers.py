"""Provider adapters (CF-2). Each returns raw text plus token usage; parsing and validation happen in the client."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from router.config.models import ProviderConfig, StepConfig


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    def complete(self, step: StepConfig, system: str, user: str, schema: dict[str, Any], name: str) -> Completion: ...


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def clean_json_text(text: str) -> str:
    """Strip reasoning tags and markdown fences some local models add around JSON."""
    text = _THINK.sub("", text).strip()
    return _FENCE.sub("", text).strip()


class OpenAICompatProvider:
    """LM Studio, OpenRouter and other OpenAI-compatible servers."""

    def __init__(self, cfg: ProviderConfig, http: httpx.Client | None = None):
        self.cfg = cfg
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = http or httpx.Client(base_url=cfg.base_url, headers=headers)

    def complete(self, step: StepConfig, system: str, user: str, schema: dict[str, Any], name: str) -> Completion:
        body: dict[str, Any] = {
            "model": step.model, "max_tokens": step.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        if step.temperature is not None:
            body["temperature"] = step.temperature
        body = _deep_merge(_deep_merge(body, self.cfg.extra), step.extra)
        try:
            resp = self.http.post("/chat/completions", json=body, timeout=step.timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timeout after {step.timeout_s}s", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"connection error: {type(exc).__name__}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}",
                                retryable=resp.status_code in (408, 429) or resp.status_code >= 500)
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        # Some servers put the answer of a thinking model in `reasoning_content` and leave `content` empty.
        text = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
        usage = data.get("usage") or {}
        return Completion(clean_json_text(text), int(usage.get("prompt_tokens") or 0),
                          int(usage.get("completion_tokens") or 0), choice.get("finish_reason"))


class AnthropicProvider:
    """Claude via the official SDK, using structured outputs (`output_config.format`)."""

    def __init__(self, cfg: ProviderConfig):
        import anthropic

        self._anthropic = anthropic
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        self.cfg = cfg
        # api_key=None lets the SDK resolve credentials itself (env, `ant auth login` profile, ...).
        self.client = anthropic.Anthropic(api_key=key, base_url=cfg.base_url, max_retries=0)

    def complete(self, step: StepConfig, system: str, user: str, schema: dict[str, Any], name: str) -> Completion:
        a = self._anthropic
        params: dict[str, Any] = {
            "model": step.model, "max_tokens": step.max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if step.temperature is not None:
            params["temperature"] = step.temperature
        params = _deep_merge(_deep_merge(params, self.cfg.extra), step.extra)
        try:
            resp = self.client.with_options(timeout=step.timeout_s).messages.create(**params)
        except a.RateLimitError as exc:
            raise ProviderError(f"rate limited: {exc}", retryable=True) from exc
        except a.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code}: {exc.message}", retryable=exc.status_code >= 500) from exc
        except a.APIConnectionError as exc:  # includes timeouts
            raise ProviderError(f"connection error: {exc}", retryable=True) from exc
        if resp.stop_reason == "refusal":
            raise ProviderError("model refused the request", retryable=False)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return Completion(text, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason)


def make_provider(cfg: ProviderConfig) -> Provider:
    return AnthropicProvider(cfg) if cfg.kind == "anthropic" else OpenAICompatProvider(cfg)


def as_json(text: str) -> Any:
    return json.loads(text)
