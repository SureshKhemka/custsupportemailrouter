"""Everything the outbound gate may treat as true for one case (FR-28, FR-31)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from router.config.models import PolicyConfig
from router.schemas.backend import Charge, Customer, Order, Refund, ReturnAuth


@dataclass(frozen=True)
class ActionRecord:
    type: str  # create_return | issue_refund | create_replacement | cancel_order
    order_id: str
    status: str  # succeeded | failed
    amount: float | None = None


@dataclass
class CaseFacts:
    language: str
    customer: Customer | None  # the verified sender, if any
    sender_display_name: str | None
    orders: list[Order]  # orders the sender is verified to own and that belong to this case
    policy: PolicyConfig
    tz: ZoneInfo
    now: datetime | None = None  # injected clock; enables "N days ago" checks
    refunds: dict[str, list[Refund]] = field(default_factory=dict)
    returns: dict[str, list[ReturnAuth]] = field(default_factory=dict)
    charges: dict[str, list[Charge]] = field(default_factory=dict)
    decision_amounts: set[float] = field(default_factory=set)  # amounts computed by policy code
    actions: list[ActionRecord] = field(default_factory=list)
    all_customers: list[Customer] = field(default_factory=list)  # registry, for other-customer data
    order_owner: dict[str, str] = field(default_factory=dict)  # any known order id -> customer id
    allowed_emails: set[str] = field(default_factory=lambda: {"support@shop.example"})  # our own addresses

    # ------------------------------------------------------------------ derived

    @property
    def order_ids(self) -> set[str]:
        return {o.order_id for o in self.orders}

    def allowed_amounts(self) -> set[float]:
        amounts = set(self.decision_amounts)
        for o in self.orders:
            amounts |= {o.total, o.subtotal, o.shipping_fee}
            for l in o.items:
                amounts |= {l.unit_price, l.unit_price * l.qty}
            refunds = [r for r in self.refunds.get(o.order_id, []) if r.status != "failed"]
            amounts |= {r.amount for r in refunds}
            if refunds:
                amounts.add(sum(r.amount for r in refunds))
            amounts |= {c.amount for c in self.charges.get(o.order_id, [])}
        amounts |= {a.amount for a in self.actions if a.amount is not None and a.status == "succeeded"}
        amounts.add(self.policy.damage.photo_required_above_item_value)  # policy thresholds quoted to customers
        return {round(a, 2) for a in amounts if a}

    def dates(self) -> dict[str, set[date]]:
        """Known dates by meaning, as calendar dates in the business timezone."""
        d: dict[str, set[date]] = {k: set() for k in ("delivered", "shipped", "placed", "promised", "cancelled",
                                                      "refund", "pickup", "tracking")}

        def add(kind: str, dt) -> None:
            if dt is not None:
                d[kind].add(dt.astimezone(self.tz).date())

        for o in self.orders:
            add("delivered", o.delivered_at)
            add("shipped", o.shipped_at)
            add("placed", o.placed_at)
            add("promised", o.promised_delivery_date)
            add("cancelled", o.cancelled_at)
            for e in o.tracking_events:
                add("tracking", e.at)
            for r in self.refunds.get(o.order_id, []):
                add("refund", r.created_at)
                add("refund", r.processed_at)
            for r in self.returns.get(o.order_id, []):
                add("pickup", r.pickup_scheduled_for)
        return d
