"""Identity and facts (FR-7..FR-11, FR-16 repeat contact). Code only, backed by HTTP calls.

Nothing about an order is fetched beyond the order record itself until ownership is verified;
refunds, returns and payments are loaded only for verified orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from router.clients import Backends
from router.config.models import Settings
from router.decide.identity import OrderResolution, Ownership, ownership, resolve_orders
from router.decide.signals import repeat_contact_count
from router.schemas.backend import Charge, Customer, Order, Refund, ReturnAuth
from router.schemas.email import InboundEmail
from router.store.db import Store


@dataclass
class OrderFacts:
    order: Order
    refunds: list[Refund] = field(default_factory=list)
    returns: list[ReturnAuth] = field(default_factory=list)
    charges: list[Charge] = field(default_factory=list)


@dataclass
class Identity:
    customer: Customer | None
    mentioned_order_ids: list[str]
    unknown_order_ids: list[str]
    ownership: Ownership
    resolution: OrderResolution
    verified: dict[str, OrderFacts]  # order_id -> facts; only orders the sender owns
    recent_order_ids: list[str]
    contact_count: int
    inherited_from_case: bool = False  # order ids taken from earlier emails in the thread (FR-4)

    @property
    def is_verified(self) -> bool:
        return self.ownership == "owner"

    @property
    def primary_order_id(self) -> str | None:
        return self.resolution.order_ids[0] if self.resolution.order_ids else None


def identify(email: InboundEmail, case_id: str, mentioned: list[str], backends: Backends, store: Store,
             cfg: Settings, now: datetime, *, order_bound: bool = True, item_hints: list[str] = ()) -> Identity:
    """`order_bound`: whether any intent is about a specific order. Only then is an order inferred
    from the customer's recent orders when the email names none (FR-8)."""
    customer = backends.customer.by_email(email.sender)

    inherited = False
    if not mentioned:  # a follow-up that doesn't repeat the order id refers to the case's orders (FR-4)
        earlier = [oid for e in store.case_emails(case_id) for oid in e.order_ids]
        if earlier:
            mentioned, inherited = list(dict.fromkeys(earlier)), True

    found = {oid: backends.order.get(oid) for oid in mentioned}
    referenced = [o for o in found.values() if o is not None]
    unknown = [oid for oid, o in found.items() if o is None]
    own = ownership(customer, referenced)

    recent: list[Order] = []
    if customer is not None and own == "owner":
        recent = backends.order.by_customer(customer.customer_id,
                                            cfg.handling.ambiguous_order.candidate_window_days)

    if mentioned and not referenced:
        resolution = OrderResolution("none")  # every mentioned id is unknown
    elif referenced:
        resolution = OrderResolution("resolved", tuple(o.order_id for o in referenced))
    elif not order_bound:
        resolution = OrderResolution("not_needed")
    elif customer is not None:
        resolution = resolve_orders([], recent, list(item_hints))
    else:
        resolution = OrderResolution("none")

    verified: dict[str, OrderFacts] = {}
    if own == "owner" and resolution.status == "resolved":
        by_id = {o.order_id: o for o in [*referenced, *recent]}
        for oid in resolution.order_ids:
            order = by_id.get(oid) or backends.order.get(oid)
            if order is not None and customer is not None and order.customer_id == customer.customer_id:
                verified[oid] = OrderFacts(order, backends.refund.by_order(oid), backends.returns.by_order(oid),
                                           backends.payments.by_order(oid))

    primary = resolution.order_ids[0] if resolution.order_ids else None
    count = 1
    if customer is not None and own == "owner":
        window_start = now - timedelta(days=cfg.escalation.repeat_contact.window_days)
        earlier = [e.received_at for e in store.emails_from(email.sender, window_start)
                   if e.outcome in {"new_case", "follow_up"} and e.received_at < email.received_at
                   and (e.case_id == case_id or (primary and primary in e.order_ids))]
        count = repeat_contact_count(primary, email.received_at, customer.contact_history, earlier, cfg)

    return Identity(customer, mentioned, unknown, own, resolution, verified, [o.order_id for o in recent], count,
                    inherited)
