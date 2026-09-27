"""Global operating mode per category (FR-22): shadow, draft_only, live.

Applied after the case decision. It can only make the outcome more cautious:
- live:       as decided.
- draft_only: anything AUTO becomes DRAFT, and no action runs automatically.
- shadow:     everything is processed and recorded, but nothing is sent and no action runs.
The case uses the most cautious mode among its intents.
"""

from __future__ import annotations

from dataclasses import dataclass

from router.config.models import Settings
from router.decide.routing import CaseHandling

OP_RANK = {"live": 0, "draft_only": 1, "shadow": 2}


@dataclass(frozen=True)
class Delivery:
    operating_mode: str
    mode: str  # case mode after the operating mode
    send_reply: bool  # write the reply to the outbox now (still subject to the gate)
    run_actions_for: tuple[str, ...]
    hold_actions_for: tuple[str, ...]
    shadow: bool  # record what would have happened, change nothing


def effective_operating_mode(intents: list[str], cfg: Settings) -> str:
    om = cfg.routing.operating_mode
    modes = [om.per_intent.get(i, om.default) for i in intents] or [om.default]
    return max(modes, key=OP_RANK.__getitem__)


def apply_operating_mode(h: CaseHandling, cfg: Settings) -> Delivery:
    op = effective_operating_mode(list(h.intent_modes), cfg)
    mode = h.mode
    automatic_reply = mode == "AUTO"  # auto_replied, identity_reply, clarification_requested
    if op == "live":
        return Delivery(op, mode, automatic_reply, h.run_actions_for, h.hold_actions_for, False)
    held = h.run_actions_for + h.hold_actions_for
    if op == "draft_only":
        return Delivery(op, "DRAFT" if mode == "AUTO" else mode, False, (), held, False)
    return Delivery(op, mode, False, (), held, True)
