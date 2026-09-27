"""Escalation signals (FR-16). Tone signals come from the LLM with a confidence; code applies the
thresholds and adds the signals only code can know (VIP tier, order value, repeat contact)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from router.config.models import Settings
from router.schemas.backend import ContactRecord, Customer, Order


@dataclass(frozen=True)
class ToneSignals:
    """Confidences (0-1) reported by the understanding step."""

    anger: float = 0.0
    chargeback_threat: float = 0.0
    public_complaint_threat: float = 0.0


def repeat_contact_count(order_id: str | None, received_at: datetime, contact_history: Iterable[ContactRecord],
                         earlier_case_emails: Iterable[datetime], cfg: Settings) -> int:
    """This contact + earlier contacts about the same order (customer history) + earlier emails in
    the same case, within the configured window."""
    start = received_at - timedelta(days=cfg.escalation.repeat_contact.window_days)
    count = 1
    if order_id:
        count += sum(1 for h in contact_history if h.order_id == order_id and start <= h.at < received_at)
    count += sum(1 for t in earlier_case_emails if start <= t < received_at)
    return count


def compute_signals(tone: ToneSignals, customer: Customer | None, verified: bool, verified_orders: list[Order],
                    contact_count: int, cfg: Settings) -> frozenset[str]:
    """`verified` = the sender owns every referenced order (FR-9). VIP, value and repeat-contact
    signals only apply to a verified sender, so nobody can borrow another customer's status."""
    esc = cfg.escalation
    signals = set()
    if tone.anger >= esc.anger_min_confidence:
        signals.add("anger")
    if tone.chargeback_threat >= esc.chargeback_or_public_threat_min_confidence:
        signals.add("chargeback_threat")
    if tone.public_complaint_threat >= esc.chargeback_or_public_threat_min_confidence:
        signals.add("public_complaint_threat")
    if customer is not None and verified:
        if customer.tier in esc.vip_tiers:
            signals.add("vip")
        if any(o.total > esc.high_order_value.threshold for o in verified_orders):
            signals.add("high_value")
        if contact_count >= esc.repeat_contact.min_contacts:
            signals.add("repeat_contact")
    return frozenset(signals)
