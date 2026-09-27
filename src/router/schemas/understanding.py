"""Structured output of the `understand` LLM step (FR-12..FR-14, FR-16 tone, FR-17 injection). Validated (LL-3)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentOut(_M):
    intent: str
    confidence: float = Field(ge=0, le=1)
    evidence: str
    order_ids: list[str]
    items: list[str]
    amounts: list[float]
    dates: list[str]
    requested_remedy: Literal["refund", "replacement", "exchange", "none"]


class ToneOut(_M):
    anger: float = Field(ge=0, le=1)
    chargeback_threat: float = Field(ge=0, le=1)
    public_complaint_threat: float = Field(ge=0, le=1)


class UnderstandingOut(_M):
    language: str
    code_mixed: bool
    intents: list[IntentOut] = Field(min_length=1)
    tone: ToneOut
    injection_attempt: bool
    injection_evidence: str


_LANG = re.compile(r"^[a-z]{2}$")


def validate_understanding(out: UnderstandingOut, taxonomy: set[str]) -> list[str]:
    """Checks beyond the schema. Any problem makes the output invalid (retry, then human)."""
    problems = []
    if not _LANG.match(out.language):
        problems.append(f"language must be an ISO 639-1 code, got {out.language!r}")
    for i in out.intents:
        if i.intent not in taxonomy:
            problems.append(f"unknown intent {i.intent!r}")
        if not i.evidence.strip():
            problems.append(f"intent {i.intent} has no evidence quote")
    return problems


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def evidence_found(evidence: str, text: str) -> bool:
    """Is the quoted evidence really in the email? Tolerates case, punctuation and spacing changes."""
    norm = lambda s: _WS.sub(" ", _PUNCT.sub(" ", s.lower())).strip()  # noqa: E731
    ev, body = norm(evidence), norm(text)
    if not ev:
        return False
    if ev in body:
        return True
    words = ev.split()
    return len(words) >= 3 and sum(w in body.split() for w in words) / len(words) >= 0.8
