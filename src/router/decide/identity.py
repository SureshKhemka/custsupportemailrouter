"""Customer identity, order ownership and order resolution (FR-7..FR-11). Code decides, never the LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from router.schemas.backend import Customer, Order

# "ORD-100045", "ORD 100045", "ord100045", "#100045", "order 100045", "order no. 100045", "order number 100045"
_ORDER_ID = re.compile(r"(?:\bORD[-\s]?|#|\border\s+(?:no\.?\s*|number\s*|id\s*)?)(\d{6})\b", re.IGNORECASE)


def extract_order_ids(text: str) -> list[str]:
    """Order ids mentioned in free text, normalised to ORD-nnnnnn, in order of appearance, unique."""
    seen: list[str] = []
    for m in _ORDER_ID.finditer(text):
        oid = f"ORD-{m.group(1)}"
        if oid not in seen:
            seen.append(oid)
    return seen


Ownership = Literal["owner", "not_owner", "unknown_sender"]


def ownership(customer: Customer | None, orders: list[Order]) -> Ownership:
    """FR-9: the sender must own every referenced order before anything is disclosed or done."""
    if customer is None:
        return "unknown_sender"
    if any(o.customer_id != customer.customer_id for o in orders):
        return "not_owner"
    return "owner"


@dataclass(frozen=True)
class OrderResolution:
    status: Literal["resolved", "ambiguous", "none"]
    order_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = field(default=())  # for the clarifying question


def resolve_orders(mentioned: list[str], recent_orders: list[Order], item_hints: list[str] = ()) -> OrderResolution:
    """FR-8, FR-11: use mentioned ids; otherwise the only recent order; otherwise a unique item match; else ask."""
    if mentioned:
        return OrderResolution("resolved", tuple(mentioned))
    if not recent_orders:
        return OrderResolution("none")
    if len(recent_orders) == 1:
        return OrderResolution("resolved", (recent_orders[0].order_id,))
    matches = [o for o in recent_orders if _matches_items(o, item_hints)] if item_hints else []
    if len(matches) == 1:
        return OrderResolution("resolved", (matches[0].order_id,))
    return OrderResolution("ambiguous", (), tuple(o.order_id for o in recent_orders))


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "my", "of", "and", "set", "pack", "pro", "mini", "for"}


def _tokens(s: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 2}


def _matches_items(order: Order, hints: list[str]) -> bool:
    line_tokens = set().union(*(_tokens(l.name) for l in order.items))
    return any(_tokens(h) and _tokens(h) <= line_tokens for h in hints)
