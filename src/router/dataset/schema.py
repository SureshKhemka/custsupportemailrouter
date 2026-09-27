"""Labelled dataset records (DS-1, DS-4).

A record = one inbound email + its label. Records live in YAML files under
dataset/<split>/*.yaml. Dates may be relative ("@now-0.2d") to dataset.yaml's reference_now.
Emails sharing a `group` (threads, duplicates, follow-ups) are processed together, in
received_at order, with fresh state per group, so independent records never affect each other.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from router.config.invariants import REQUIRED_INTENTS

Mode = Literal["AUTO", "DRAFT", "ROUTE", "ROUTE_NO_DRAFT", "CLOSE"]
Signal = Literal["anger", "chargeback_threat", "public_complaint_threat", "repeat_contact", "vip", "high_value"]
ActionType = Literal["create_return", "issue_refund", "create_replacement", "cancel_order", "send_reply"]
Disposition = Literal["auto_replied", "drafted", "routed", "clarification_requested", "identity_reply",
                      "closed_spam", "duplicate_ignored", "merged"]
FACT_KINDS = ("order_id", "tracking", "amount", "date", "text", "name", "email")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineQty(Strict):
    line_id: str
    qty: int = Field(gt=0)


class Entities(Strict):
    order_ids: list[str] = Field(default_factory=list)  # as mentioned in the text, normalised
    items: list[str] = Field(default_factory=list)
    amounts: list[float] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    remedy: Literal["refund", "replacement", "exchange"] | None = None
    photos_attached: bool = False


class ExpectedIntent(Strict):
    intent: str
    order_id: str | None = None
    mode: Mode
    decision: dict[str, Any] = Field(default_factory=dict)
    entities: Entities = Field(default_factory=Entities)

    @field_validator("intent")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in REQUIRED_INTENTS:
            raise ValueError(f"unknown intent {v!r}")
        return v


class Fact(Strict):
    kind: Literal["order_id", "tracking", "amount", "date", "text", "name", "email"]
    value: str | float

    @model_validator(mode="before")
    @classmethod
    def _from_single_key(cls, data: Any) -> Any:
        # YAML shorthand: {amount: 2499} -> {kind: amount, value: 2499}
        if isinstance(data, dict) and len(data) == 1 and next(iter(data)) in FACT_KINDS:
            (k, v), = data.items()
            return {"kind": k, "value": v}
        return data


class ReplyExpectation(Strict):
    must_contain: list[Fact] = Field(default_factory=list)
    must_not_contain: list[Fact] = Field(default_factory=list)


class ExpectedAction(Strict):
    type: ActionType
    order_id: str
    lines: list[LineQty] = Field(default_factory=list)
    amount: float | None = None


class CaseExpectation(Strict):
    mode: Mode
    disposition: Disposition
    queue: str | None = None
    priority: bool = False


class Label(Strict):
    language: str = "en"
    code_mixed: bool = False
    intents: list[ExpectedIntent] = Field(min_length=1)
    order: str = "not_needed"  # ORD-..., "ambiguous", "none", "not_needed"
    ownership: Literal["owner", "not_owner", "unknown_sender", "not_applicable"] = "owner"
    escalation: list[Signal] = Field(default_factory=list)
    injection: bool = False
    case: CaseExpectation
    actions: list[ExpectedAction] = Field(default_factory=list)
    proposed_actions: list[ExpectedAction] = Field(default_factory=list)
    forbidden_actions: list[ActionType] = Field(default_factory=list)
    reply: ReplyExpectation = Field(default_factory=ReplyExpectation)
    notes: str = ""

    @field_validator("order")
    @classmethod
    def _order_ref(cls, v: str) -> str:
        if not (v.startswith("ORD-") or v in {"ambiguous", "none", "not_needed"}):
            raise ValueError("order must be an ORD- id, 'ambiguous', 'none' or 'not_needed'")
        return v

    @property
    def intent_names(self) -> set[str]:
        return {i.intent for i in self.intents}


class Record(Strict):
    id: str = Field(pattern=r"^[A-Z]\d{3,4}[a-z]?$")
    group: str | None = None
    tags: list[str] = Field(default_factory=list)
    email: dict[str, Any]
    label: Label

    @property
    def group_id(self) -> str:
        return self.group or self.id


class DatasetManifest(Strict):
    version: str
    reference_now: str
    description: str = ""


# --------------------------------------------------------------------------- DS-6 / DS-7 reply sets

GateCheck = Literal["facts_match_backend", "claimed_actions_succeeded", "no_other_customer_data",
                    "no_payment_details", "language_matches", "no_placeholders", "consistent_with_policy"]


class RecordedAction(Strict):
    type: ActionType
    order_id: str
    amount: float | None = None
    status: Literal["succeeded", "failed"]


class GateCase(Strict):
    """DS-6: a reply the outbound gate must block (or, for controls, pass)."""

    id: str = Field(pattern=r"^[BG]\d{3}$")
    record: str
    defect: str
    expect: Literal["block", "pass"]
    checks: list[GateCheck] = Field(default_factory=list)
    recorded_actions: list[RecordedAction] | None = None  # None = use the actions implied by the record's label
    reply: str
    notes: str = ""

    @model_validator(mode="after")
    def _checks_match_expectation(self) -> GateCase:
        if self.expect == "block" and not self.checks:
            raise ValueError("a 'block' case must name the gate checks that should fail")
        if self.expect == "pass" and self.checks:
            raise ValueError("a 'pass' control must not list failing checks")
        return self


class RatedReply(Strict):
    """DS-7: a reply with (human) quality ratings, to measure judge agreement."""

    id: str = Field(pattern=r"^R\d{3}$")
    record: str
    reply: str
    ratings: dict[str, int]
    overall: int = Field(ge=1, le=5)
    reason: str
    rated_by: str

    @field_validator("ratings")
    @classmethod
    def _scale(cls, v: dict[str, int]) -> dict[str, int]:
        bad = {k: x for k, x in v.items() if not 1 <= x <= 5}
        if bad:
            raise ValueError(f"ratings must be 1-5, got {bad}")
        return v

    @property
    def is_draft(self) -> bool:
        return self.rated_by.startswith("claude")
