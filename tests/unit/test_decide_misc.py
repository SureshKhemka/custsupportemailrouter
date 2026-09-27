"""Operating mode (FR-22), SLA (FR-37), identity / order resolution (FR-7..FR-11), signals (FR-16)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.decide.identity import extract_order_ids, ownership, resolve_orders
from router.decide.operating_mode import apply_operating_mode, effective_operating_mode
from router.decide.routing import CaseInput, IntentInput, decide_case
from router.decide.signals import ToneSignals, compute_signals, repeat_contact_count
from router.decide.sla import sla_due, sla_status
from router.schemas.backend import ContactRecord, Customer, Order

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


def cfg_with(tmp_path: Path, data: dict | None = None):
    overlays = []
    if data:
        p = tmp_path / "o.yaml"
        p.write_text(yaml.safe_dump(data))
        overlays.append(p)
    return load_config(overlays, root=ROOT, use_local=False, env={}).settings


def order(oid: str, cid: str = "C1", total: float = 1000, names: tuple[str, ...] = ("Item",)) -> Order:
    return Order.model_validate({
        "order_id": oid, "customer_id": cid, "status": "in_transit", "currency": "INR", "subtotal": total,
        "shipping_fee": 0, "total": total, "placed_at": NOW - timedelta(days=2),
        "items": [{"line_id": f"{oid}-L{i}", "sku": "S", "name": n, "category": "home", "qty": 1, "unit_price": total}
                  for i, n in enumerate(names, 1)]})


CUST = Customer(customer_id="C1", name="A B", emails=["a@x.com"], tier="standard")
VIP = Customer(customer_id="C1", name="A B", emails=["a@x.com"], tier="vip")


# --------------------------------------------------------------------------- operating mode


def _auto_return(cfg):
    return decide_case(CaseInput(intents=(IntentInput("return_request"),)), cfg)


def test_live_keeps_decision(tmp_path) -> None:
    cfg = cfg_with(tmp_path, {"routing": {"operating_mode": {"default": "live"}}})
    d = apply_operating_mode(_auto_return(cfg), cfg)
    assert (d.mode, d.send_reply, d.run_actions_for, d.shadow) == ("AUTO", True, ("return_request",), False)


def test_draft_only_turns_auto_into_draft_and_holds_actions(tmp_path) -> None:
    cfg = cfg_with(tmp_path, {"routing": {"operating_mode": {"default": "draft_only"}}})
    d = apply_operating_mode(_auto_return(cfg), cfg)
    assert (d.mode, d.send_reply, d.run_actions_for, d.hold_actions_for) == ("DRAFT", False, (), ("return_request",))


def test_shadow_changes_nothing(tmp_path) -> None:
    cfg = cfg_with(tmp_path)  # base default is shadow (D-006)
    d = apply_operating_mode(_auto_return(cfg), cfg)
    assert (d.shadow, d.send_reply, d.run_actions_for, d.mode) == (True, False, (), "AUTO")


def test_most_cautious_mode_wins_per_case(tmp_path) -> None:
    cfg = cfg_with(tmp_path, {"routing": {"operating_mode": {"default": "live",
                                                             "per_intent": {"return_request": "draft_only"}}}})
    assert effective_operating_mode(["order_status"], cfg) == "live"
    assert effective_operating_mode(["order_status", "return_request"], cfg) == "draft_only"


# --------------------------------------------------------------------------- SLA


def test_sla_due_and_status(tmp_path) -> None:
    cfg = cfg_with(tmp_path, {"sla": {"per_intent": {"legal_threat": 2}}})
    due = sla_due(NOW, ["order_status", "legal_threat"], cfg)
    assert due == NOW + timedelta(hours=2)
    assert sla_status(NOW, due, NOW + timedelta(minutes=30), cfg) == "ok"
    assert sla_status(NOW, due, NOW + timedelta(minutes=90), cfg) == "approaching"
    assert sla_status(NOW, due, NOW + timedelta(hours=2, seconds=1), cfg) == "breached"
    assert sla_due(NOW, ["order_status"], cfg) == NOW + timedelta(hours=4)


# --------------------------------------------------------------------------- identity


@pytest.mark.parametrize("text,ids", [
    ("Where is ORD-100045?", ["ORD-100045"]),
    ("order 100045 and #100046, also ord 100045", ["ORD-100045", "ORD-100046"]),
    ("order no. 100047 / order number 100048", ["ORD-100047", "ORD-100048"]),
    ("I paid 100045 rupees", []),
    ("call me on 9811100045", []),
])
def test_extract_order_ids(text, ids) -> None:
    assert extract_order_ids(text) == ids


def test_ownership() -> None:
    assert ownership(None, []) == "unknown_sender"
    assert ownership(CUST, [order("O1", "C1")]) == "owner"
    assert ownership(CUST, [order("O1", "C1"), order("O2", "C2")]) == "not_owner"


def test_resolve_orders() -> None:
    lamp, sheet = order("O1", names=("LED Desk Lamp",)), order("O2", names=("Cotton Bedsheet Set",))
    assert resolve_orders(["O9"], [lamp, sheet]).order_ids == ("O9",)
    assert resolve_orders([], []).status == "none"
    assert resolve_orders([], [lamp]).order_ids == ("O1",)
    assert resolve_orders([], [lamp, sheet], ["desk lamp"]).order_ids == ("O1",)
    amb = resolve_orders([], [lamp, sheet], ["parcel"])
    assert (amb.status, amb.candidates) == ("ambiguous", ("O1", "O2"))


# --------------------------------------------------------------------------- signals


def test_tone_thresholds_and_customer_signals(tmp_path) -> None:
    cfg = cfg_with(tmp_path)
    big = order("O1", total=60000)
    s = compute_signals(ToneSignals(anger=0.8, chargeback_threat=0.5), VIP, True, [big], 2, cfg)
    assert s == {"anger", "vip", "high_value", "repeat_contact"}
    assert compute_signals(ToneSignals(anger=0.69), CUST, True, [order("O2")], 1, cfg) == frozenset()


def test_unverified_sender_cannot_borrow_vip_or_value(tmp_path) -> None:
    cfg = cfg_with(tmp_path)
    assert compute_signals(ToneSignals(), VIP, False, [order("O1", total=60000)], 5, cfg) == frozenset()


def test_repeat_contact_window(tmp_path) -> None:
    cfg = cfg_with(tmp_path)
    hist = [ContactRecord(at=NOW - timedelta(days=d), channel="email", order_id=oid, topic="t", summary="s")
            for d, oid in [(2, "O1"), (5, "O1"), (20, "O1"), (1, "O2")]]
    assert repeat_contact_count("O1", NOW, hist, [], cfg) == 3
    assert repeat_contact_count("O1", NOW, hist, [NOW - timedelta(hours=3)], cfg) == 4
    assert repeat_contact_count(None, NOW, hist, [], cfg) == 1
