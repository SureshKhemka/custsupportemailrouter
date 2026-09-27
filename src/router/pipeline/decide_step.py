"""Decide (FR-15..FR-23, FR-37): per-intent policy decisions from backend facts, then the case decision.

Everything here is code. The LLM's understanding only supplies intents, item names and tone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from router.clients import Backends
from router.config.models import Settings
from router.decide import policy as P
from router.decide.operating_mode import Delivery, apply_operating_mode
from router.decide.routing import CaseHandling, CaseInput, IntentInput, decide_case
from router.decide.signals import compute_signals
from router.decide.sla import sla_due
from router.pipeline.identify import Identity, OrderFacts
from router.pipeline.understanding import Understanding
from router.schemas.email import InboundEmail


@dataclass
class IntentDecision:
    intent: str
    order_id: str | None
    conditions: set[str] = field(default_factory=set)
    decision: dict[str, Any] = field(default_factory=dict)
    lines: list[tuple[str, int]] = field(default_factory=list)
    action: dict[str, Any] | None = None  # the action this intent would take (run now, held, or proposed)


@dataclass
class Decision:
    handling: CaseHandling
    delivery: Delivery
    intents: list[IntentDecision]
    signals: frozenset[str]
    sla_due: datetime | None
    merged_into: str | None = None


# --------------------------------------------------------------------------- lines


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "my", "of", "and", "set", "pack", "pair", "pairs", "both", "two", "one", "item", "items",
         "order", "from", "for", "new"}


def _tokens(s: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 2}


def select_lines(order, hints: list[str]) -> list[P.LineRequest]:
    """Order lines the customer named (remaining quantity), or every line when none match (FR-14)."""
    matched = []
    for line in order.items:
        lt = _tokens(line.name) | _tokens(line.category)
        if any(_tokens(h) and (_tokens(h) & lt) for h in hints):
            left = line.qty - line.returned_qty
            matched.append(P.LineRequest(line.line_id, left if left > 0 else line.qty))
    return matched or P.whole_lines(order) or [P.LineRequest(l.line_id, l.qty) for l in order.items]


# --------------------------------------------------------------------------- per-intent decisions


def _order_for_intent(intent_order_ids: tuple[str, ...], identity: Identity) -> str | None:
    for oid in intent_order_ids:
        if oid in identity.verified:
            return oid
    return identity.primary_order_id if identity.primary_order_id in identity.verified else None


def decide_intents(u: Understanding, identity: Identity, email: InboundEmail, backends: Backends, cfg: Settings,
                   now: datetime) -> list[IntentDecision]:
    tz = ZoneInfo(cfg.app.timezone)
    out: list[IntentDecision] = []
    details = list(u.details) or []
    per_intent = [(d.intent, d.order_ids, list(d.items)) for d in details] or [(i, (), list(u.item_hints))
                                                                             for i in u.intents]
    seen: set[tuple[str, str | None]] = set()
    for intent, order_ids, items in per_intent:
        oid = _order_for_intent(order_ids, identity) if identity.is_verified else None
        if (intent, oid) in seen:
            continue
        seen.add((intent, oid))
        d = IntentDecision(intent, oid)
        facts: OrderFacts | None = identity.verified.get(oid) if oid else None
        if facts is not None:
            _decide_one(d, facts, items or list(u.item_hints), email, backends, cfg, now, tz)
        out.append(d)
    return out


def _decide_one(d: IntentDecision, f: OrderFacts, hints: list[str], email: InboundEmail, backends: Backends,
                cfg: Settings, now: datetime, tz: ZoneInfo) -> None:
    order = f.order
    if d.intent == "order_status":
        state = P.delivery_state(order, now, cfg.policy, tz)
        d.decision = {"delivery_state": state}
        if state == "lost":
            d.conditions.add("lost_shipment")
    elif d.intent == "return_request":
        lines = select_lines(order, hints)
        r = P.return_eligibility(order, lines, now, cfg.policy, tz)
        d.decision = {"eligible": r.eligible, "reason": r.reason, "days_since_delivery": r.days_since_delivery}
        d.lines = [(l.line_id, l.qty) for l in lines]
        if r.eligible:
            d.action = {"type": "create_return", "order_id": order.order_id, "lines": d.lines, "reason": "customer_request"}
        else:
            d.conditions.add("not_eligible")
    elif d.intent in {"damaged_item", "wrong_item"}:
        lines = select_lines(order, hints)
        stock = {}
        for l in lines:
            sku = order.line(l.line_id).sku
            p = backends.order.product(sku)
            stock[sku] = bool(p and p.in_stock)
        kind = "damaged" if d.intent == "damaged_item" else "wrong_item"
        r = P.damage_remedy(order, lines, kind, now, stock, email.has_photos, cfg.policy, tz)  # type: ignore[arg-type]
        d.decision = {"remedy": r.remedy, "reason": r.reason, "photo_required": r.photo_required,
                      "refund_amount": r.refund_amount, "days_since_delivery": r.days_since_delivery}
        d.lines = [(l.line_id, l.qty) for l in lines]
        if r.remedy == "replacement":
            d.action = {"type": "create_replacement", "order_id": order.order_id, "lines": d.lines, "reason": kind}
        elif r.remedy == "refund":
            d.action = {"type": "issue_refund", "order_id": order.order_id, "amount": r.refund_amount, "reason": kind}
    elif d.intent == "cancel_order":
        ok = P.can_cancel(order, cfg.policy)
        d.decision = {"cancellable": ok}
        if ok:
            d.action = {"type": "cancel_order", "order_id": order.order_id, "reason": "customer_request"}
        else:
            d.conditions.add("already_shipped")
    elif d.intent == "refund_status":
        rs = P.refund_status(f.refunds)
        d.decision = {"state": rs.state, "amount": rs.amount}
    elif d.intent in {"billing_dispute", "payment_issue"}:
        d.decision = {"duplicate_charge": sum(c.status == "succeeded" for c in f.charges) >= 2,
                      "failed_payment": any(c.status == "failed" for c in f.charges)}


# --------------------------------------------------------------------------- case decision


def decide(u: Understanding, identity: Identity, email: InboundEmail, backends: Backends, cfg: Settings,
           now: datetime, *, merged: bool = False) -> Decision:
    intents = decide_intents(u, identity, email, backends, cfg, now) if not u.failed else []
    verified_orders = [f.order for f in identity.verified.values()]
    signals = compute_signals(u.tone, identity.customer, identity.is_verified, verified_orders,
                              identity.contact_count, cfg)
    resolution = identity.resolution.status
    inp = CaseInput(
        intents=tuple(IntentInput(d.intent, frozenset(d.conditions)) for d in intents),
        language_supported=u.language in cfg.app.supported_languages,
        ownership=identity.ownership,
        order_resolution=resolution,  # type: ignore[arg-type]
        signals=signals, injection=u.injection, merged=merged,
        uncertain=bool(u.uncertain), understanding_failed=u.failed,
    )
    handling = decide_case(inp, cfg)
    delivery = apply_operating_mode(handling, cfg)
    due = sla_due(email.received_at, list(u.intents) or ["other"], cfg) if handling.mode != "CLOSE" else None
    return Decision(handling, delivery, intents, signals, due)
