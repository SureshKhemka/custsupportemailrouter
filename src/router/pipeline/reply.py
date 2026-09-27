"""Reply composition from templates (FR-21, FR-27..FR-30) and gate facts (FR-31).

One reply per case covering all intents. Every fact comes from backend records or policy decisions
(FR-28); templates are per language (FR-29). M8 adds optional LLM personalisation on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from router.clients import Backends
from router.config.models import Settings
from router.pipeline.actions import ActionResult
from router.pipeline.decide_step import Decision, IntentDecision
from router.pipeline.identify import Identity
from router.pipeline.understanding import Understanding
from router.schemas.backend import Order

BILLING = {"billing_dispute", "payment_issue"}
GENERAL = {"product_question", "complaint", "other"}
NO_DRAFT = {"legal_threat", "abuse", "spam_or_auto"}


def inr(amount: float) -> str:
    """₹ with Indian digit grouping: 123456.5 -> ₹1,23,456.50."""
    whole, frac = divmod(round(amount * 100), 100)
    s = str(int(whole))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head, *groups] if head else groups) + "," + tail
    return f"₹{s}" + (f".{frac:02d}" if frac else "")


def fmt_date(dt: datetime | None, tz: ZoneInfo) -> str | None:
    if dt is None:
        return None
    d = dt.astimezone(tz)
    return f"{d.day} {d:%B %Y}"


@dataclass(frozen=True)
class ComposedReply:
    text: str
    kind: str  # auto | draft | identity | clarify


class Composer:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.app.timezone)
        self._envs: dict[str, Environment] = {}

    def env(self, lang: str) -> Environment:
        if lang not in self._envs:
            path = Path(self.cfg.paths.templates) / lang
            self._envs[lang] = Environment(loader=FileSystemLoader(str(path)), undefined=StrictUndefined,
                                           trim_blocks=True, lstrip_blocks=True, autoescape=False)
        return self._envs[lang]

    def compose(self, decision: Decision, u: Understanding, identity: Identity, actions: list[ActionResult],
                backends: Backends, now: datetime) -> ComposedReply | None:
        h = decision.handling
        lang = u.language if u.language in self.cfg.app.supported_languages else None
        if lang is None or h.mode in {"CLOSE", "ROUTE_NO_DRAFT"}:
            return None
        env = self.env(lang)
        first = identity.customer.name.split()[0] if identity.customer and identity.is_verified else None
        if h.disposition == "identity_reply":
            return ComposedReply(self._wrap(env, None, [env.get_template("identity.j2").render()]), "identity")
        if h.disposition == "clarification_requested":
            cands = [o for oid in identity.resolution.candidates if (o := backends.order.get(oid))]
            text = env.get_template("clarify.j2").render(candidates=[
                {"order_id": o.order_id, "items": self._items(o), "placed": fmt_date(o.placed_at, self.tz)} for o in cands])
            return ComposedReply(self._wrap(env, first, [text]), "clarify")

        by_intent: dict[tuple[str, str | None], ActionResult] = {(a.intent, a.order_id): a for a in actions}
        parts: list[str] = []
        general_done = False
        for d in decision.intents:
            if d.intent in NO_DRAFT:
                continue
            if d.intent in GENERAL:
                if not general_done:
                    parts.append(env.get_template("general.j2").render())
                    general_done = True
                continue
            order = identity.verified[d.order_id].order if d.order_id in identity.verified else None
            if d.intent in BILLING:
                parts.append(env.get_template("billing.j2").render(o=self._o(order) if order else None))
                continue
            if order is None:
                continue
            text = self._intent_part(env, d, order, by_intent.get((d.intent, d.order_id)), now)
            if text.strip():
                parts.append(text)
        if not parts:
            parts.append(env.get_template("general.j2").render())
        kind = "auto" if decision.delivery.mode == "AUTO" else "draft"
        return ComposedReply(self._wrap(env, first, parts), kind)

    # ------------------------------------------------------------------ helpers

    def _wrap(self, env: Environment, first: str | None, parts: list[str]) -> str:
        return env.get_template("reply.j2").render(first_name=first, parts=parts).strip() + "\n"

    def _items(self, o: Order) -> str:
        return ", ".join(f"{l.name}" + (f" x{l.qty}" if l.qty > 1 else "") for l in o.items)

    def _o(self, o: Order, now: datetime | None = None) -> dict[str, Any]:
        tz = self.tz
        promised_passed = bool(now and o.promised_delivery_date and
                               o.promised_delivery_date.astimezone(tz).date() < now.astimezone(tz).date())
        return {"order_id": o.order_id, "items": self._items(o), "carrier": o.carrier or "our courier",
                "tracking": o.tracking_number, "promised": fmt_date(o.promised_delivery_date, tz),
                "promised_passed": promised_passed, "delivered": fmt_date(o.delivered_at, tz),
                "cancelled": fmt_date(o.cancelled_at, tz), "last_update": fmt_date(o.last_tracking_update, tz)}

    def _intent_part(self, env: Environment, d: IntentDecision, order: Order, action: ActionResult | None,
                     now: datetime) -> str:
        o = self._o(order, now)
        policy = self.cfg.policy
        lines_text = ", ".join(order.line(l).name + (f" x{q}" if q > 1 else "") for l, q in d.lines) if d.lines else ""
        act = None if action is None else ("held" if action.status in {"proposed", "shadow"} else action.status)
        tpl = env.get_template(f"{d.intent}.j2")
        dec = d.decision
        if d.intent == "order_status":
            return tpl.render(o=o, state=dec["delivery_state"])
        if d.intent == "return_request":
            cats = [order.line(l).category for l, _ in d.lines if order.line(l).category in policy.returns.non_returnable_categories]
            return tpl.render(o=o, action=act, reason=dec["reason"], lines_text=lines_text, policy=policy,
                              category=cats[0] if cats else "these")
        if d.intent in {"damaged_item", "wrong_item"}:
            return tpl.render(o=o, remedy=dec["remedy"], reason=dec["reason"], lines_text=lines_text, policy=policy,
                              kind="damaged" if d.intent == "damaged_item" else "wrong",
                              amount=inr(dec["refund_amount"]) if dec.get("refund_amount") else None,
                              photo_threshold=inr(policy.damage.photo_required_above_item_value))
        if d.intent == "cancel_order":
            returnable = all(l.category not in policy.returns.non_returnable_categories for l in order.items)
            return tpl.render(o=o, action=act, cancellable=dec["cancellable"], returnable=returnable, policy=policy)
        if d.intent == "refund_status":
            return tpl.render(o=o, state=dec["state"], amount=inr(dec["amount"]) if dec["amount"] else None)
        return ""
