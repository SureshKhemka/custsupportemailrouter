"""Build outbound-gate facts for a labelled record from seed data (used by DS-6 / HG-5 evals and tests)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from router.config.models import Settings
from router.dataset.check import SeedView
from router.dataset.loader import LoadedRecord
from router.dataset.schema import RecordedAction
from router.decide.identity import ownership
from router.gate.facts import ActionRecord, CaseFacts


def facts_for_record(lr: LoadedRecord, seed: SeedView, cfg: Settings,
                     recorded_actions: list[RecordedAction] | None = None) -> CaseFacts:
    label = lr.record.label
    customer = seed.customer_by_email(lr.email.sender)
    referenced = [seed.orders[i.order_id] for i in label.intents if i.order_id in seed.orders]
    verified = ownership(customer, referenced) == "owner"
    orders = list({o.order_id: o for o in referenced}.values()) if verified else []
    ids = [o.order_id for o in orders]
    if recorded_actions is None:  # default: the record's expected actions, all succeeded
        actions = [ActionRecord(a.type, a.order_id, "succeeded", a.amount) for a in label.actions]
    else:
        actions = [ActionRecord(a.type, a.order_id, a.status, a.amount) for a in recorded_actions]
    decision_amounts = {float(i.decision["refund_amount"]) for i in label.intents if "refund_amount" in i.decision}
    return CaseFacts(
        language=label.language,
        customer=customer if verified else None,
        sender_display_name=lr.email.from_.name,
        orders=orders,
        policy=cfg.policy,
        tz=ZoneInfo(cfg.app.timezone),
        now=lr.email.received_at,
        refunds={i: seed.refunds.get(i, []) for i in ids},
        returns={i: seed.returns.get(i, []) for i in ids},
        charges={i: seed.charges.get(i, []) for i in ids},
        decision_amounts=decision_amounts if verified else set(),
        actions=actions,
        all_customers=seed.customers,
        order_owner={o.order_id: o.customer_id for o in seed.orders.values()},
    )
