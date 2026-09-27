"""Rules configuration can never override (FR-19, CF-4).

These live in code on purpose: they are the safety floor under the routing matrix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from router.config.models import RoutingConfig

# Intents the taxonomy must contain at minimum (FR-12).
REQUIRED_INTENTS: frozenset[str] = frozenset({
    "order_status", "return_request", "refund_status", "damaged_item", "wrong_item",
    "billing_dispute", "payment_issue", "cancel_order", "product_question", "complaint",
    "legal_threat", "abuse", "spam_or_auto", "other",
})

# FR-19: never AUTO, under any condition.
NEVER_AUTO: frozenset[str] = frozenset({"billing_dispute", "payment_issue", "legal_threat", "abuse"})

# FR-19: never drafted. The only allowed mode is ROUTE_NO_DRAFT.
NEVER_DRAFTED: frozenset[str] = frozenset({"legal_threat", "abuse"})

# Closing without a reply is only acceptable for spam and automated mail (FR-6).
CLOSE_ALLOWED: frozenset[str] = frozenset({"spam_or_auto"})

# Queues that must exist (FR-35).
REQUIRED_QUEUES: frozenset[str] = frozenset({"general", "billing", "legal", "abuse", "gate_failures"})

# Code-evaluated conditions the matrix may refer to.
KNOWN_CONDITIONS: frozenset[str] = frozenset({"lost_shipment", "not_eligible", "already_shipped"})


def routing_invariant_violations(routing: RoutingConfig) -> list[str]:
    """Return a human-readable message for every invariant the routing config breaks."""
    errors: list[str] = []
    for intent, rule in sorted(routing.matrix.items()):
        modes = {rule.mode, *rule.conditions.values()}
        if intent in NEVER_AUTO and "AUTO" in modes:
            errors.append(f"FR-19: intent '{intent}' can never be AUTO (found in mode or conditions)")
        if intent in NEVER_DRAFTED and modes != {"ROUTE_NO_DRAFT"}:
            errors.append(
                f"FR-19: intent '{intent}' can never be drafted; its only allowed mode is "
                f"ROUTE_NO_DRAFT (found {sorted(modes)})"
            )
        if "CLOSE" in modes and intent not in CLOSE_ALLOWED:
            errors.append(f"intent '{intent}' cannot be CLOSE; only {sorted(CLOSE_ALLOWED)} may be closed without reply")
    return errors
