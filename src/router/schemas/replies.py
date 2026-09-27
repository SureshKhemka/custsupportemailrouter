"""LLM outputs for reply personalisation, agent summaries and judging (LL-3: validated)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComposeOut(_M):
    reply: str = Field(min_length=20)


class SummaryOut(_M):
    summary: str = Field(min_length=20)
    customer_asks: list[str]
    key_facts: list[str]
    risk_flags: list[str]
    suggested_next_step: str


class Score(_M):
    score: int = Field(ge=1, le=5)
    reason: str


class JudgeOut(_M):
    correctness: Score
    completeness: Score
    tone: Score
    clarity: Score
    language: Score
    overall: Score


class SummaryJudgeOut(_M):
    accuracy: Score
    completeness: Score
    overall: Score
