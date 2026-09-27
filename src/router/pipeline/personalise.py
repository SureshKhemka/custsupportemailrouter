"""LLM personalisation of templated replies (FR-27, FR-29) and agent summaries (LL-1).

The LLM only rephrases. Code checks that every protected fact in the template survives verbatim;
the result still has to pass the outbound gate, and on any problem the template is used.
Summaries may only mention order ids, tracking numbers and amounts present in the facts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from router.config.models import Settings
from router.gate import CaseFacts
from router.gate import extract as X
from router.llm import LLMClient, LLMFailure
from router.schemas.email import InboundEmail
from router.schemas.replies import ComposeOut, SummaryOut

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "es": "Spanish", "de": "German"}
_DATE_TEXT = re.compile(r"\b\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|"
                        r"November|December) \d{4}\b")
_AMOUNT_TEXT = re.compile(r"₹[\d,]+(?:\.\d{2})?")


def protected_facts(text: str) -> list[str]:
    """Strings the rewrite must keep character for character."""
    found = [*X.extract_order_ids(text), *X.tracking_numbers(text), *_AMOUNT_TEXT.findall(text), *_DATE_TEXT.findall(text)]
    greeting = X.greeting_name(text)
    if greeting:
        found.append(greeting)
    return list(dict.fromkeys(found))


def should_personalise(kind: str, intents: tuple[str, ...], cfg: Settings) -> bool:
    p = cfg.replies.personalise
    if kind in {"identity", "clarify"}:
        return False  # fixed safety wording
    overrides = [p.per_intent[i] for i in intents if i in p.per_intent]
    if overrides:
        return all(overrides)
    return p.auto if kind == "auto" else p.draft


@dataclass(frozen=True)
class Personalised:
    text: str
    used_llm: bool
    note: str | None = None


class Personaliser:
    def __init__(self, llm: LLMClient, cfg: Settings):
        self.llm, self.cfg = llm, cfg
        self.tone_guide = cfg.paths.tone_guide.read_text(encoding="utf-8") if cfg.paths.tone_guide.is_file() else ""

    def __call__(self, draft: str, email: InboundEmail, language: str) -> Personalised:
        facts = protected_facts(draft)

        def validate(out: ComposeOut) -> list[str]:
            missing = [f for f in facts if f not in out.reply]
            return [f"these facts must appear exactly as written: {missing}"] if missing else []

        try:
            res = self.llm.run("compose", ComposeOut, {
                "language_name": LANGUAGE_NAMES.get(language, language), "tone_guide": self.tone_guide,
                "subject": email.subject, "body": email.body, "draft": draft.strip(), "facts": facts},
                validate=validate)
        except LLMFailure as exc:
            return Personalised(draft, False, f"personalisation failed, template used: {exc}")
        text = res.parsed.reply.strip() + "\n"
        return Personalised(text, True)


def facts_payload(f: CaseFacts, decisions: list[dict[str, Any]] | None = None) -> str:
    """Backend facts as compact JSON for the summariser and the judge."""
    tz: ZoneInfo = f.tz

    def d(x):
        return x.astimezone(tz).strftime("%d %B %Y") if x else None

    orders = []
    for o in f.orders:
        orders.append({
            "order_id": o.order_id, "status": o.status,
            "items": [{"name": l.name, "category": l.category, "qty": l.qty, "unit_price": l.unit_price,
                       "returned_qty": l.returned_qty} for l in o.items],
            "total": o.total, "shipping_fee": o.shipping_fee, "placed": d(o.placed_at), "shipped": d(o.shipped_at),
            "promised_delivery": d(o.promised_delivery_date), "delivered": d(o.delivered_at),
            "carrier": o.carrier, "tracking_number": o.tracking_number, "last_tracking_update": d(o.last_tracking_update),
            "refunds": [{"amount": r.amount, "status": r.status, "created": d(r.created_at)} for r in f.refunds.get(o.order_id, [])],
            "returns": [{"status": r.status, "created": d(r.created_at)} for r in f.returns.get(o.order_id, [])],
            "charges": [{"amount": c.amount, "status": c.status} for c in f.charges.get(o.order_id, [])],
        })
    body = {
        "customer": {"name": f.customer.name, "tier": f.customer.tier} if f.customer else "sender not verified",
        "today": d(f.now) if f.now else None, "currency": f.policy.currency,
        "policy": {"return_window_days": f.policy.returns.window_days,
                   "non_returnable_categories": f.policy.returns.non_returnable_categories,
                   "damage_report_window_days": f.policy.damage.report_window_days},
        "orders": orders,
        "recorded_actions": [{"type": a.type, "order_id": a.order_id, "amount": a.amount,
                              "status": "proposed, awaiting agent approval" if a.status == "not_run" else a.status}
                             for a in f.actions],
        "policy_decisions": decisions or [],
    }
    return json.dumps(body, ensure_ascii=False, indent=1)


class Summarizer:
    def __init__(self, llm: LLMClient, cfg: Settings):
        self.llm, self.cfg = llm, cfg

    def __call__(self, email: InboundEmail, facts: CaseFacts, reason: str,
                 decisions: list[dict[str, Any]]) -> SummaryOut | None:
        allowed_ids = {o.order_id for o in facts.orders}
        allowed_tracking = {o.tracking_number for o in facts.orders if o.tracking_number}
        allowed_amounts = facts.allowed_amounts()
        email_text = f"{email.subject}\n{email.body}"

        def validate(out: SummaryOut) -> list[str]:
            text = " ".join([out.summary, *out.key_facts, out.suggested_next_step])
            bad = [o for o in X.extract_order_ids(text) if o not in allowed_ids and o[4:] not in email_text]
            bad += [t for t in X.tracking_numbers(text) if t not in allowed_tracking and t not in email_text]
            bad += [str(a) for a in X.amounts(text) if not any(abs(a - b) < 0.01 for b in allowed_amounts)
                    and str(int(a)) not in email_text.replace(",", "")]
            return [f"not in the facts or the email: {bad}"] if bad else []

        try:
            return self.llm.run("summarize", SummaryOut, {
                "reason": reason, "sender": f"{email.from_.name or ''} <{email.sender}>", "subject": email.subject,
                "body": email.body, "facts": facts_payload(facts, decisions)}, validate=validate).parsed
        except LLMFailure:
            return None
