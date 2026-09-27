"""Deterministic policy decisions and their boundaries (FR-23, PO-1..PO-5)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from router.config import load_config
from router.decide import policy as P
from router.schemas.backend import Order, Refund

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")
TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def policy():
    return load_config(root=ROOT, use_local=False, env={}).settings.policy


def order(status="delivered", delivered_days=5.0, category="apparel", price=1999.0, qty=1, shipping=0.0,
          returned=0, promised_days=None, last_event_days=None, **kw) -> Order:
    items = [{"line_id": "O-L1", "sku": "SKU-1", "name": "Item", "category": category, "qty": qty,
              "unit_price": price, "returned_qty": returned}]
    events = []
    if last_event_days is not None:
        events = [{"at": NOW - timedelta(days=last_event_days), "status": "in_transit", "location": "Hub",
                   "description": "x"}]
    subtotal = price * qty
    return Order.model_validate({
        "order_id": "O", "customer_id": "C", "status": status, "items": items, "subtotal": subtotal,
        "shipping_fee": shipping, "total": subtotal + shipping, "currency": "INR",
        "placed_at": NOW - timedelta(days=40),
        "shipped_at": NOW - timedelta(days=10),
        "delivered_at": NOW - timedelta(days=delivered_days) if status == "delivered" else None,
        "promised_delivery_date": NOW + timedelta(days=promised_days) if promised_days is not None else None,
        "tracking_events": events, **kw,
    })


def lines(o: Order, qty: int = 1) -> list[P.LineRequest]:
    return [P.LineRequest("O-L1", qty)]


# --------------------------------------------------------------------------- calendar days


def test_calendar_days_use_business_timezone() -> None:
    late_evening_utc = datetime.fromisoformat("2026-09-26T19:00:00+00:00")  # 27 Sep 00:30 IST
    assert P.calendar_days(late_evening_utc, NOW, TZ) == 0
    assert P.calendar_days(late_evening_utc, NOW, ZoneInfo("UTC")) == 1


# --------------------------------------------------------------------------- PO-1 returns


@pytest.mark.parametrize("days,eligible", [(29, True), (30, True), (31, False)])
def test_return_window_boundary(policy, days, eligible) -> None:
    o = order(delivered_days=days)
    d = P.return_eligibility(o, lines(o), NOW, policy, TZ)
    assert d.eligible is eligible and d.reason == ("eligible" if eligible else "outside_window")


@pytest.mark.parametrize("category", ["perishable", "personalised", "hygiene"])
def test_non_returnable_categories(policy, category) -> None:
    o = order(category=category, delivered_days=1)
    assert P.return_eligibility(o, lines(o), NOW, policy, TZ).reason == "non_returnable"


def test_return_not_delivered_or_already_returned(policy) -> None:
    o = order(status="in_transit")
    assert P.return_eligibility(o, lines(o), NOW, policy, TZ).reason == "not_delivered"
    o = order(status="return_in_progress")
    assert P.return_eligibility(o, lines(o), NOW, policy, TZ).reason == "already_returned"


def test_return_quantity_left(policy) -> None:
    o = order(qty=2, returned=1)
    assert P.return_eligibility(o, lines(o, 1), NOW, policy, TZ).eligible
    assert P.return_eligibility(o, lines(o, 2), NOW, policy, TZ).reason == "nothing_left_to_return"
    assert P.whole_lines(o) == [P.LineRequest("O-L1", 1)]


# --------------------------------------------------------------------------- PO-4 delivery


def test_delayed_and_lost_boundaries(policy) -> None:
    assert P.delivery_state(order(status="in_transit", promised_days=1, last_event_days=1), NOW, policy, TZ) == "in_transit"
    assert P.delivery_state(order(status="in_transit", promised_days=-1, last_event_days=1), NOW, policy, TZ) == "delayed"
    assert P.delivery_state(order(status="in_transit", promised_days=-5, last_event_days=6.9), NOW, policy, TZ) == "delayed"
    assert P.delivery_state(order(status="in_transit", promised_days=-5, last_event_days=7), NOW, policy, TZ) == "lost"
    assert P.delivery_state(order(status="placed"), NOW, policy, TZ) == "not_shipped"
    assert P.delivery_state(order(status="out_for_delivery", promised_days=0, last_event_days=0.2),
                            NOW, policy, TZ) == "out_for_delivery"


# --------------------------------------------------------------------------- PO-2 / PO-3 damage


@pytest.mark.parametrize("days,reason", [(6, "ok"), (7, "ok"), (8, "reported_late")])
def test_damage_window_boundary(policy, days, reason) -> None:
    o = order(delivered_days=days, price=1000)
    d = P.damage_remedy(o, lines(o), "damaged", NOW, {"SKU-1": True}, False, policy, TZ)
    assert d.reason == reason


def test_photo_threshold_is_strictly_above(policy) -> None:
    at = order(price=2000)
    above = order(price=2001)
    assert P.damage_remedy(at, lines(at), "damaged", NOW, {"SKU-1": True}, False, policy, TZ).remedy == "replacement"
    d = P.damage_remedy(above, lines(above), "damaged", NOW, {"SKU-1": True}, False, policy, TZ)
    assert (d.remedy, d.reason) == ("request_photo", "photo_required")
    assert P.damage_remedy(above, lines(above), "damaged", NOW, {"SKU-1": True}, True, policy, TZ).remedy == "replacement"


def test_out_of_stock_refund_includes_shipping_once(policy) -> None:
    o = order(price=499, qty=2, shipping=79)
    d = P.damage_remedy(o, lines(o, 2), "wrong_item", NOW, {"SKU-1": False}, False, policy, TZ)
    assert (d.remedy, d.refund_amount) == ("refund", 499 * 2 + 79)


def test_refund_amount_without_shipping_for_other_reasons(policy) -> None:
    o = order(price=499, shipping=79)
    assert P.refund_amount(o, [(o.items[0], 1)], "changed_mind", policy) == 499


# --------------------------------------------------------------------------- PO-5 / refunds


@pytest.mark.parametrize("status,ok", [("placed", True), ("packed", True), ("shipped", False), ("delivered", False)])
def test_can_cancel(policy, status, ok) -> None:
    assert P.can_cancel(order(status=status), policy) is ok


def test_refund_status_latest_state_and_total() -> None:
    assert P.refund_status([]) == P.RefundStatus("none", 0.0)
    refunds = [Refund(refund_id="1", order_id="O", amount=100, status="processed", reason="x", created_at=NOW - timedelta(days=5)),
               Refund(refund_id="2", order_id="O", amount=50, status="initiated", reason="x", created_at=NOW - timedelta(days=1)),
               Refund(refund_id="3", order_id="O", amount=70, status="failed", reason="x", created_at=NOW - timedelta(days=3))]
    assert P.refund_status(refunds) == P.RefundStatus("initiated", 150.0)
