"""Case-level handling decision (FR-10, FR-11, FR-15..FR-20). Pure and deterministic.

Inputs are already-established facts (intents with their code-evaluated conditions,
escalation signals, identity and order-resolution results). Output is the case mode,
disposition, queue and which actions run now versus wait for a human.
The operating mode (shadow / draft_only / live, FR-22) is applied afterwards, separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from router.config.models import Settings

MODE_RANK = {"CLOSE": 0, "AUTO": 1, "DRAFT": 2, "ROUTE": 3, "ROUTE_NO_DRAFT": 4}
QUEUE_RANK = {"general": 0, "billing": 1, "abuse": 2, "legal": 3}

# Intents that are about a specific order and therefore need verified ownership (FR-9).
ORDER_BOUND = frozenset({"order_status", "return_request", "refund_status", "damaged_item", "wrong_item",
                         "cancel_order"})
# Intents with an action that may run automatically.
AUTO_ACTIONS = {"return_request": "create_return", "cancel_order": "cancel_order"}

Ownership = Literal["owner", "not_owner", "unknown_sender", "not_applicable"]
OrderResolution = Literal["resolved", "ambiguous", "none", "not_needed"]
Disposition = Literal["auto_replied", "drafted", "routed", "clarification_requested", "identity_reply",
                      "closed_spam", "duplicate_ignored", "merged"]


def strictest(modes: list[str]) -> str:
    return max(modes, key=MODE_RANK.__getitem__)


@dataclass(frozen=True)
class IntentInput:
    intent: str
    conditions: frozenset[str] = frozenset()  # e.g. {"not_eligible"}, evaluated by policy code


@dataclass(frozen=True)
class CaseInput:
    intents: tuple[IntentInput, ...]
    language_supported: bool = True
    ownership: Ownership = "owner"
    order_resolution: OrderResolution = "resolved"
    signals: frozenset[str] = frozenset()
    injection: bool = False
    duplicate: bool = False  # same message id seen before (FR-2)
    merged: bool = False  # near-duplicate merged into an open case (FR-3)
    uncertain: bool = False  # some intent below its confidence threshold (FR-15)
    understanding_failed: bool = False  # no valid LLM output after retries (LL-3)


@dataclass(frozen=True)
class CaseHandling:
    mode: str
    disposition: Disposition
    queue: str | None
    priority: bool
    intent_modes: dict[str, str]
    escalated: bool = False
    flags: tuple[str, ...] = ()
    run_actions_for: tuple[str, ...] = ()
    hold_actions_for: tuple[str, ...] = field(default=())


def intent_mode(intent: str, conditions: frozenset[str], cfg: Settings) -> str:
    rule = cfg.routing.matrix[intent]
    return strictest([rule.mode, *(rule.conditions[c] for c in conditions if c in rule.conditions)])


def decide_case(inp: CaseInput, cfg: Settings) -> CaseHandling:
    if inp.duplicate:
        return CaseHandling("CLOSE", "duplicate_ignored", None, False, {})
    if inp.merged:
        return CaseHandling("CLOSE", "merged", None, False, {})
    if inp.understanding_failed:  # LL-3: degrade to a human, never to a guess
        return CaseHandling("ROUTE", "routed", "general", False, {}, flags=("understanding_failed",))

    intents = [i for i in inp.intents if i.intent != "spam_or_auto"] or list(inp.intents)
    if not intents:
        intents = [IntentInput("other")]
    modes: dict[str, str] = {}
    for i in intents:  # the same intent may appear for several orders; keep the strictest
        m = intent_mode(i.intent, i.conditions, cfg)
        modes[i.intent] = strictest([modes[i.intent], m]) if i.intent in modes else m
    if set(modes) == {"spam_or_auto"} and not inp.uncertain:  # an uncertain "spam" is never dropped
        return CaseHandling("CLOSE", "closed_spam", None, False, modes)

    mode = strictest(list(modes.values()))
    flags: list[str] = []
    escalated = False

    if not inp.language_supported:
        mode = strictest([mode, "ROUTE"])
        flags.append("unsupported_language")
    if inp.uncertain:  # FR-15: a human sees the model's best guess
        mode = strictest([mode, "ROUTE"])
        flags.append("uncertain_intent")

    escalate = bool(inp.signals) or (inp.injection and cfg.handling.injection.on_detect == "escalate")
    if inp.injection:
        flags.append("prompt_injection")
    if escalate and mode == "AUTO":
        per = cfg.handling.escalation.per_intent
        mode = strictest([per.get(i, cfg.handling.escalation.auto_becomes) for i in modes])
        escalated = True

    order_bound = any(i in ORDER_BOUND for i in modes)
    needs_human = MODE_RANK[mode] >= MODE_RANK["ROUTE"]

    # FR-10: unverified sender on an order-bound request.
    if order_bound and inp.ownership in {"not_owner", "unknown_sender"}:
        flags.append(inp.ownership)
        if not needs_human:
            if cfg.handling.identity.on_unverified == "template_reply":
                return CaseHandling("AUTO", "identity_reply", None, False, modes, escalated, tuple(flags))
            mode = "ROUTE"

    # FR-11: several candidate orders and no way to tell which.
    elif order_bound and inp.order_resolution == "ambiguous" and not needs_human:
        flags.append("ambiguous_order")
        if mode == "AUTO" and cfg.handling.ambiguous_order.on_ambiguous == "clarify":
            return CaseHandling("AUTO", "clarification_requested", None, False, modes, escalated, tuple(flags))
        if cfg.handling.ambiguous_order.on_ambiguous == "route":
            mode = "ROUTE"

    # FR-8: order-bound request but the customer has no matching order at all.
    elif order_bound and inp.order_resolution == "none" and not needs_human:
        flags.append("no_matching_order")
        mode = "ROUTE"

    disposition: Disposition = {"AUTO": "auto_replied", "DRAFT": "drafted"}.get(mode, "routed")  # type: ignore[assignment]
    queue = None
    if mode != "AUTO":
        queues = [cfg.routing.matrix[i].queue for i in modes if cfg.routing.matrix[i].queue]
        queue = max(queues, key=lambda q: QUEUE_RANK.get(q, 0)) if queues else "general"
    priority = mode != "AUTO" and any(cfg.routing.matrix[i].priority for i in modes)

    run, hold = _actions(modes, mode, escalated, inp, cfg)
    return CaseHandling(mode, disposition, queue, priority, modes, escalated, tuple(flags), run, hold)


def _actions(modes: dict[str, str], case_mode: str, escalated: bool, inp: CaseInput,
             cfg: Settings) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Which AUTO-able actions run now, and which wait for a human (FR-20, handling.multi_intent)."""
    candidates = [i for i, m in modes.items() if m == "AUTO" and i in AUTO_ACTIONS]
    if case_mode == "AUTO":
        return tuple(candidates), ()
    verified = inp.ownership == "owner" and inp.order_resolution == "resolved"
    mi = cfg.handling.multi_intent
    run = [i for i in candidates
           if verified and not escalated and mi.per_intent.get(i, mi.actions_when_case_not_auto) == "run"]
    return tuple(run), tuple(i for i in candidates if i not in run)
