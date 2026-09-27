"""Deterministic policy decisions (FR-23, PO-1..PO-5). No LLM ever calls into or overrides these.

All functions are pure: facts in (backend records, policy config, "now"), decision out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from router.config.models import PolicyConfig
from router.schemas.backend import Order, OrderLine, Refund

DeliveryState = Literal["not_shipped", "in_transit", "out_for_delivery", "delayed", "lost", "delivered", "cancelled",
                        "return_in_progress", "returned", "refunded"]


def calendar_days(earlier: datetime, later: datetime, tz: ZoneInfo) -> int:
    """Whole calendar days between two instants, in the business timezone."""
    return (later.astimezone(tz).date() - earlier.astimezone(tz).date()).days


def elapsed_days(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


# --------------------------------------------------------------------------- delivery (PO-4)


def delivery_state(order: Order, now: datetime, policy: PolicyConfig, tz: ZoneInfo) -> DeliveryState:
    if order.status in {"placed", "packed"}:
        return "not_shipped"
    if order.status in {"delivered", "cancelled", "return_in_progress", "returned", "refunded"}:
        return order.status  # type: ignore[return-value]
    # shipped / in_transit / out_for_delivery
    last = order.last_tracking_update or order.shipped_at
    if last and elapsed_days(last, now) >= policy.delivery.lost_after_days_without_update:
        return "lost"
    if order.promised_delivery_date and calendar_days(order.promised_delivery_date, now, tz) > 0:
        return "delayed"
    return "out_for_delivery" if order.status == "out_for_delivery" else "in_transit"


# --------------------------------------------------------------------------- returns (PO-1)

ReturnReason = Literal["eligible", "not_delivered", "already_returned", "non_returnable", "outside_window",
                       "nothing_left_to_return"]


@dataclass(frozen=True)
class LineRequest:
    line_id: str
    qty: int


@dataclass(frozen=True)
class ReturnDecision:
    eligible: bool
    reason: ReturnReason
    days_since_delivery: int | None
    lines: tuple[LineRequest, ...] = ()


def return_eligibility(order: Order, lines: list[LineRequest], now: datetime, policy: PolicyConfig,
                       tz: ZoneInfo) -> ReturnDecision:
    if order.status in {"return_in_progress", "returned", "refunded"}:
        return ReturnDecision(False, "already_returned", _days_since_delivery(order, now, tz))
    if order.status != "delivered" or order.delivered_at is None:
        return ReturnDecision(False, "not_delivered", None)
    days = _days_since_delivery(order, now, tz)
    requested = [(order.line(l.line_id), l.qty) for l in lines]
    if any(line.category in policy.returns.non_returnable_categories for line, _ in requested):
        return ReturnDecision(False, "non_returnable", days)
    if days is None or days > policy.returns.window_days:
        return ReturnDecision(False, "outside_window", days)
    if any(qty > line.qty - line.returned_qty for line, qty in requested):
        return ReturnDecision(False, "nothing_left_to_return", days)
    return ReturnDecision(True, "eligible", days, tuple(lines))


def _days_since_delivery(order: Order, now: datetime, tz: ZoneInfo) -> int | None:
    return calendar_days(order.delivered_at, now, tz) if order.delivered_at else None


def whole_lines(order: Order) -> list[LineRequest]:
    """Every line with its remaining quantity (used when the customer does not name items)."""
    return [LineRequest(l.line_id, l.qty - l.returned_qty) for l in order.items if l.qty - l.returned_qty > 0]


# --------------------------------------------------------------------------- damaged / wrong item (PO-2, PO-3)

Remedy = Literal["replacement", "refund", "request_photo", "none"]
DamageReason = Literal["ok", "not_delivered", "reported_late", "photo_required"]


@dataclass(frozen=True)
class DamageDecision:
    remedy: Remedy
    reason: DamageReason
    days_since_delivery: int | None
    photo_required: bool
    refund_amount: float | None = None
    lines: tuple[LineRequest, ...] = field(default=())


def damage_remedy(order: Order, lines: list[LineRequest], kind: Literal["damaged", "wrong_item"], now: datetime,
                  in_stock: dict[str, bool], photos_attached: bool, policy: PolicyConfig,
                  tz: ZoneInfo) -> DamageDecision:
    if order.delivered_at is None:
        return DamageDecision("none", "not_delivered", None, False)
    days = calendar_days(order.delivered_at, now, tz)
    affected = [(order.line(l.line_id), l.qty) for l in lines]
    photo_required = any(line.unit_price > policy.damage.photo_required_above_item_value for line, _ in affected)
    if days > policy.damage.report_window_days:
        return DamageDecision("none", "reported_late", days, photo_required)
    if photo_required and not photos_attached:
        return DamageDecision("request_photo", "photo_required", days, True, lines=tuple(lines))
    for remedy in policy.damage.remedy_order:
        if remedy == "replacement" and all(in_stock.get(line.sku, False) for line, _ in affected):
            return DamageDecision("replacement", "ok", days, photo_required, lines=tuple(lines))
        if remedy == "refund":
            amount = refund_amount(order, affected, kind, policy)
            return DamageDecision("refund", "ok", days, photo_required, amount, tuple(lines))
    return DamageDecision("none", "ok", days, photo_required)


def refund_amount(order: Order, affected: list[tuple[OrderLine, int]], reason: str, policy: PolicyConfig) -> float:
    """PO-2: item price paid, plus the order's shipping fee (once) for damaged / wrong / never-arrived."""
    amount = sum(line.unit_price * qty for line, qty in affected)
    if reason in policy.refunds.include_shipping_for:
        amount += order.shipping_fee
    return round(amount, 2)


# --------------------------------------------------------------------------- cancellation (PO-5)


def can_cancel(order: Order, policy: PolicyConfig) -> bool:
    return order.status in policy.cancellation.allowed_statuses


# --------------------------------------------------------------------------- refund status


@dataclass(frozen=True)
class RefundStatus:
    state: Literal["none", "initiated", "processed", "failed"]
    amount: float


def refund_status(refunds: list[Refund]) -> RefundStatus:
    """Report the latest refund's state; amount is the sum of non-failed refunds."""
    if not refunds:
        return RefundStatus("none", 0.0)
    latest = max(refunds, key=lambda r: r.created_at)
    return RefundStatus(latest.status, round(sum(r.amount for r in refunds if r.status != "failed"), 2))
