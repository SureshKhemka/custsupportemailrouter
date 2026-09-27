"""Typed, validated configuration (CF-1, CF-4).

Every model forbids unknown keys, so a typo in a YAML file stops startup instead of
being silently ignored.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from router.config.invariants import (
    KNOWN_CONDITIONS,
    REQUIRED_INTENTS,
    REQUIRED_QUEUES,
    routing_invariant_violations,
)

HandlingMode = Literal["AUTO", "DRAFT", "ROUTE", "ROUTE_NO_DRAFT", "CLOSE"]
OperatingMode = Literal["shadow", "draft_only", "live"]
ProviderKind = Literal["openai_compat", "anthropic"]

LLM_STEPS: frozenset[str] = frozenset({"understand", "summarize", "compose", "judge"})
SERVICES: frozenset[str] = frozenset({"customer", "order", "returns", "refund", "payments", "replacement", "outbox"})

_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LANG_CODE = re.compile(r"^[a-z]{2}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------- app / paths


class AppSection(Strict):
    name: str
    supported_languages: list[str] = Field(min_length=1)
    timezone: str

    @field_validator("supported_languages")
    @classmethod
    def _iso_codes(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if not _LANG_CODE.match(c)]
        if bad:
            raise ValueError(f"language codes must be ISO 639-1 lowercase (e.g. 'en'), got {bad}")
        return v


class ClockConfig(Strict):
    # FR-38 / NF-2: a fixed "now" for deterministic runs. null = real time.
    fixed_now: datetime | None = None

    @field_validator("fixed_now")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("fixed_now must include a UTC offset, e.g. 2026-09-27T10:00:00+05:30")
        return v


class IntakeConfig(Strict):
    near_duplicate_window_minutes: int = Field(gt=0)


class SlaConfig(Strict):
    default_hours: float = Field(gt=0)
    per_intent: dict[str, float] = Field(default_factory=dict)
    approaching_fraction: float = Field(gt=0, lt=1)

    @field_validator("per_intent")
    @classmethod
    def _positive(cls, v: dict[str, float]) -> dict[str, float]:
        bad = {k: h for k, h in v.items() if h <= 0}
        if bad:
            raise ValueError(f"SLA hours must be > 0, got {bad}")
        return v


class PathsConfig(Strict):
    inbox: Path
    outbox: Path
    db_dir: Path
    logs: Path
    reports: Path
    dataset: Path
    seed: Path
    recordings: Path
    prompts: Path
    templates: Path
    tone_guide: Path
    rubrics: Path
    mock_state: Path

    def resolved(self, root: Path) -> PathsConfig:
        return PathsConfig(**{k: (v if v.is_absolute() else root / v) for k, v in self})


# --------------------------------------------------------------------------- LLM


class ProviderConfig(Strict):
    kind: ProviderKind
    base_url: str
    # Name of the environment variable holding the key -- never the key itself (CF-3).
    api_key_env: str | None = None

    @field_validator("api_key_env")
    @classmethod
    def _looks_like_env_var(cls, v: str | None) -> str | None:
        if v is not None and not _ENV_VAR_NAME.match(v):
            raise ValueError(
                "api_key_env must be the NAME of an environment variable (e.g. ANTHROPIC_API_KEY), "
                "not a key value"
            )
        return v


class StepConfig(Strict):
    provider: str
    model: str
    prompt_version: str
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)


class ModelPricing(Strict):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class LlmConfig(Strict):
    providers: dict[str, ProviderConfig] = Field(min_length=1)
    defaults: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, StepConfig]
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _apply_step_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("steps"), dict):
            defaults = data.get("defaults") or {}
            data = {**data, "steps": {
                name: ({**defaults, **step} if isinstance(step, dict) else step)
                for name, step in data["steps"].items()
            }}
        return data

    @field_validator("defaults")
    @classmethod
    def _known_default_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        allowed = {"temperature", "max_tokens", "timeout_s", "retries"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown llm.defaults keys {sorted(unknown)}; allowed: {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _steps_and_providers(self) -> LlmConfig:
        missing = LLM_STEPS - set(self.steps)
        unknown = set(self.steps) - LLM_STEPS
        if missing:
            raise ValueError(f"llm.steps is missing required steps {sorted(missing)}")
        if unknown:
            raise ValueError(f"llm.steps has unknown steps {sorted(unknown)}; known: {sorted(LLM_STEPS)}")
        for name, step in self.steps.items():
            if step.provider not in self.providers:
                raise ValueError(
                    f"llm.steps.{name}.provider '{step.provider}' is not defined in llm.providers "
                    f"({sorted(self.providers)})"
                )
        return self


# --------------------------------------------------------------------------- taxonomy / routing


class IntentConfig(Strict):
    threshold: float = Field(gt=0, le=1)
    description: str


class TaxonomyConfig(Strict):
    intents: dict[str, IntentConfig]

    @model_validator(mode="after")
    def _required(self) -> TaxonomyConfig:
        missing = REQUIRED_INTENTS - set(self.intents)
        if missing:
            raise ValueError(f"taxonomy is missing required intents {sorted(missing)}")
        return self


class RouteRule(Strict):
    mode: HandlingMode
    conditions: dict[str, HandlingMode] = Field(default_factory=dict)
    queue: str | None
    priority: bool = False

    @model_validator(mode="after")
    def _shape(self) -> RouteRule:
        unknown = set(self.conditions) - KNOWN_CONDITIONS
        if unknown:
            raise ValueError(f"unknown conditions {sorted(unknown)}; known: {sorted(KNOWN_CONDITIONS)}")
        if self.mode == "CLOSE" and self.queue is not None:
            raise ValueError("CLOSE rules must have queue: null")
        if self.mode != "CLOSE" and self.queue is None:
            raise ValueError(f"mode {self.mode} needs a queue")
        return self


class RefundIssuanceConfig(Strict):
    # None = always DRAFT. A number = AUTO strictly below that amount (policy currency).
    auto_below_amount: float | None = Field(default=None, ge=0)


class ActionsConfig(Strict):
    refund_issuance: RefundIssuanceConfig


class OperatingModeConfig(Strict):
    default: OperatingMode
    per_intent: dict[str, OperatingMode] = Field(default_factory=dict)


class RoutingConfig(Strict):
    queues: list[str]
    matrix: dict[str, RouteRule]
    actions: ActionsConfig
    operating_mode: OperatingModeConfig

    @model_validator(mode="after")
    def _queues_and_invariants(self) -> RoutingConfig:
        missing_q = REQUIRED_QUEUES - set(self.queues)
        if missing_q:
            raise ValueError(f"routing.queues is missing required queues {sorted(missing_q)}")
        bad_q = {i: r.queue for i, r in self.matrix.items() if r.queue is not None and r.queue not in self.queues}
        if bad_q:
            raise ValueError(f"routing.matrix refers to undefined queues {bad_q}")
        violations = routing_invariant_violations(self)
        if violations:
            raise ValueError("; ".join(violations))
        return self


# --------------------------------------------------------------------------- handling choices

# Actions that move no money and may run while the rest of the case waits for a human.
NON_MONETARY_AUTO_INTENTS: frozenset[str] = frozenset({"return_request", "cancel_order"})


class EscalationHandling(Strict):
    auto_becomes: Literal["DRAFT", "ROUTE"]
    per_intent: dict[str, Literal["DRAFT", "ROUTE"]] = Field(default_factory=dict)


class MultiIntentHandling(Strict):
    actions_when_case_not_auto: Literal["hold", "run"]
    per_intent: dict[str, Literal["hold", "run"]] = Field(default_factory=dict)

    @field_validator("per_intent")
    @classmethod
    def _non_monetary_only(cls, v: dict[str, str]) -> dict[str, str]:
        bad = set(v) - NON_MONETARY_AUTO_INTENTS
        if bad:
            raise ValueError(f"only {sorted(NON_MONETARY_AUTO_INTENTS)} may be overridden, got {sorted(bad)}")
        return v


class InjectionHandling(Strict):
    on_detect: Literal["escalate", "flag_only"]


class IdentityHandling(Strict):
    on_unverified: Literal["template_reply", "route"]


class AmbiguousOrderHandling(Strict):
    on_ambiguous: Literal["clarify", "route"]
    candidate_window_days: int = Field(gt=0)


class HandlingConfig(Strict):
    escalation: EscalationHandling
    multi_intent: MultiIntentHandling
    injection: InjectionHandling
    identity: IdentityHandling
    ambiguous_order: AmbiguousOrderHandling


# --------------------------------------------------------------------------- escalation / policy


class RepeatContactConfig(Strict):
    min_contacts: int = Field(ge=2)
    window_days: int = Field(gt=0)


class HighOrderValueConfig(Strict):
    threshold: float = Field(gt=0)


class EscalationConfig(Strict):
    repeat_contact: RepeatContactConfig
    vip_tiers: list[str] = Field(min_length=1)
    high_order_value: HighOrderValueConfig
    anger_min_confidence: float = Field(gt=0, le=1)
    chargeback_or_public_threat_min_confidence: float = Field(gt=0, le=1)


class ReturnsPolicy(Strict):
    window_days: int = Field(gt=0)
    non_returnable_categories: list[str]


class RefundsPolicy(Strict):
    to_original_payment_method: Literal[True]
    include_shipping_for: list[Literal["damaged", "wrong_item", "never_arrived"]]


class DamagePolicy(Strict):
    report_window_days: int = Field(gt=0)
    remedy_order: list[Literal["replacement", "refund"]] = Field(min_length=1)
    photo_required_above_item_value: float = Field(ge=0)


class DeliveryPolicy(Strict):
    lost_after_days_without_update: int = Field(gt=0)


class CancellationPolicy(Strict):
    allowed_statuses: list[Literal["placed", "packed"]] = Field(min_length=1)


class PolicyConfig(Strict):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    returns: ReturnsPolicy
    refunds: RefundsPolicy
    damage: DamagePolicy
    delivery: DeliveryPolicy
    cancellation: CancellationPolicy


# --------------------------------------------------------------------------- services / faults


class ServiceConfig(Strict):
    base_url: str = Field(pattern=r"^https?://")
    timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0, le=10)
    backoff_s: float = Field(ge=0)


class ScriptedFault(Strict):
    endpoint: str = Field(pattern=r"^(GET|POST|PUT|PATCH|DELETE) /")
    match: dict[str, Any] = Field(default_factory=dict)
    fail_times: int = Field(ge=1)
    kind: Literal["error", "timeout", "timeout_after_commit"]


class ServiceFaults(Strict):
    error_rate: float = Field(default=0.0, ge=0, le=1)
    latency_ms: int = Field(default=0, ge=0)
    timeout_rate: float = Field(default=0.0, ge=0, le=1)
    scripted: list[ScriptedFault] = Field(default_factory=list)


class FaultsConfig(Strict):
    enabled: bool
    seed: int
    # How long a "timeout" fault hangs before answering; set above the client timeout.
    timeout_hang_s: float = Field(gt=0)
    services: dict[str, ServiceFaults] = Field(default_factory=dict)

    @field_validator("services")
    @classmethod
    def _known_services(cls, v: dict[str, ServiceFaults]) -> dict[str, ServiceFaults]:
        unknown = set(v) - SERVICES
        if unknown:
            raise ValueError(f"faults for unknown services {sorted(unknown)}; known: {sorted(SERVICES)}")
        return v


# --------------------------------------------------------------------------- evals


class JudgeConfig(Strict):
    rubrics: list[str] = Field(min_length=1)
    min_human_agreement: float = Field(gt=0, le=1)


class EvalTargets(Strict):
    intent_macro_f1: float = Field(ge=0, le=1)
    multi_intent_exact_match: float = Field(ge=0, le=1)
    order_id_accuracy: float = Field(ge=0, le=1)
    legal_recall: float = Field(ge=0, le=1)
    language_accuracy: float = Field(ge=0, le=1)
    spam_precision: float = Field(ge=0, le=1)
    agent_summary_avg: float = Field(ge=1, le=5)
    reply_judge_avg: float = Field(ge=1, le=5)
    reply_min_score_for_auto: int = Field(ge=1, le=5)
    disposition_accuracy: float = Field(ge=0, le=1)


class EvalsConfig(Strict):
    judge: JudgeConfig
    consistency_runs: int = Field(ge=1)
    targets: EvalTargets


# --------------------------------------------------------------------------- root


class Settings(Strict):
    app: AppSection
    clock: ClockConfig
    intake: IntakeConfig
    sla: SlaConfig
    paths: PathsConfig
    llm: LlmConfig
    taxonomy: TaxonomyConfig
    routing: RoutingConfig
    handling: HandlingConfig
    escalation: EscalationConfig
    policy: PolicyConfig
    services: dict[str, ServiceConfig]
    faults: FaultsConfig
    evals: EvalsConfig

    @field_validator("services")
    @classmethod
    def _all_services(cls, v: dict[str, ServiceConfig]) -> dict[str, ServiceConfig]:
        missing, unknown = SERVICES - set(v), set(v) - SERVICES
        if missing:
            raise ValueError(f"services is missing {sorted(missing)}")
        if unknown:
            raise ValueError(f"unknown services {sorted(unknown)}; known: {sorted(SERVICES)}")
        return v

    @model_validator(mode="after")
    def _cross_section(self) -> Settings:
        intents = set(self.taxonomy.intents)
        matrix = set(self.routing.matrix)
        if matrix != intents:
            raise ValueError(
                "routing.matrix must cover exactly the taxonomy intents; "
                f"missing {sorted(intents - matrix)}, unknown {sorted(matrix - intents)}"
            )
        for where, keys in {
            "handling.escalation.per_intent": set(self.handling.escalation.per_intent),
            "routing.operating_mode.per_intent": set(self.routing.operating_mode.per_intent),
            "sla.per_intent": set(self.sla.per_intent),
        }.items():
            unknown = keys - intents
            if unknown:
                raise ValueError(f"{where} refers to intents not in the taxonomy: {sorted(unknown)}")
        return self
