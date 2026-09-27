"""SLA due times and status (FR-37). Time always comes from the injected clock (FR-38)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from router.config.models import Settings

SlaStatus = Literal["ok", "approaching", "breached"]


def sla_hours(intents: list[str], cfg: Settings) -> float:
    """A case gets the tightest SLA among its intents."""
    per = cfg.sla.per_intent
    return min((per.get(i, cfg.sla.default_hours) for i in intents), default=cfg.sla.default_hours)


def sla_due(received_at: datetime, intents: list[str], cfg: Settings) -> datetime:
    return received_at + timedelta(hours=sla_hours(intents, cfg))


def sla_status(received_at: datetime, due: datetime, now: datetime, cfg: Settings) -> SlaStatus:
    if now > due:
        return "breached"
    total = (due - received_at).total_seconds()
    elapsed = (now - received_at).total_seconds()
    return "approaching" if total > 0 and elapsed / total >= cfg.sla.approaching_fraction else "ok"
