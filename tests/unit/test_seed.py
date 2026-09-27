"""Seed data coverage and integrity (SD-1..SD-6)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mocks.seed import resolve_relative

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "seed"
NOW = datetime.fromisoformat("2026-09-27T04:30:00+00:00")


def load(name: str) -> dict:
    return json.loads((SEED / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def data() -> dict:
    return {n: load(n) for n in ("catalog", "customers", "orders", "payments", "returns", "refunds", "scenarios")}


def days_ago(rel: str) -> float:
    return -float(rel.removeprefix("@now").removesuffix("d") or 0)


def test_generator_is_deterministic_and_committed_files_are_current() -> None:
    spec = importlib.util.spec_from_file_location("gen_seed", ROOT / "scripts" / "gen_seed.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_seed"] = mod  # dataclasses need the module registered
    try:
        spec.loader.exec_module(mod)
    finally:
        del sys.modules["gen_seed"]
    s = mod.build()
    assert s.orders == load("orders")["orders"], "seed/ is stale: run `uv run python scripts/gen_seed.py`"
    assert s.customers == load("customers")["customers"]
    assert s.scenarios == load("scenarios")["scenarios"]


def test_sd1_customers(data: dict) -> None:
    customers = data["customers"]["customers"]
    assert len(customers) >= 50
    assert sum(c["tier"] == "vip" for c in customers) >= 5
    assert sum(len(c["emails"]) > 1 for c in customers) >= 5
    assert sum(bool(c["contact_history"]) for c in customers) >= 3
    emails = [e.lower() for c in customers for e in c["emails"]]
    assert len(emails) == len(set(emails)), "an email is registered to two customers"


def test_sd2_every_status_and_derived_states(data: dict) -> None:
    orders = data["orders"]["orders"]
    assert len(orders) >= 150
    statuses = Counter(o["status"] for o in orders)
    for st in ["placed", "packed", "shipped", "in_transit", "out_for_delivery", "delivered", "cancelled",
               "return_in_progress", "returned", "refunded"]:
        assert statuses[st] >= 3, st
    in_transit = [o for o in orders if o["status"] == "in_transit"]
    delayed = [o for o in in_transit if days_ago(o["promised_delivery_date"]) > 0]
    lost = [o for o in in_transit if days_ago(o["tracking_events"][-1]["at"]) > 7]
    assert len(delayed) >= 3 and len(lost) >= 2


def test_sd3_window_boundaries(data: dict) -> None:
    by_id = {o["order_id"]: o for o in data["orders"]["orders"]}
    sc = data["scenarios"]["scenarios"]
    for d in (29, 30, 31):
        assert days_ago(by_id[sc[f"return_window_day_{d}"]["order_id"]]["delivered_at"]) == d
    for d in (6, 7, 8):
        assert days_ago(by_id[sc[f"damage_window_day_{d}"]["order_id"]]["delivered_at"]) == d
    assert days_ago(by_id[sc["lost_boundary_7d"]["order_id"]]["tracking_events"][-1]["at"]) == 7


def test_sd4_special_items(data: dict) -> None:
    orders = data["orders"]["orders"]
    cats = {l["category"] for o in orders for l in o["items"]}
    assert {"perishable", "personalised", "hygiene"} <= cats
    assert any(o["total"] > 50000 for o in orders)
    assert sum(len(o["items"]) > 1 for o in orders) >= 5
    assert any(o["status"] == "delivered" and any(l["returned_qty"] for l in o["items"]) for o in orders)


def test_sd5_payments(data: dict) -> None:
    charges = data["payments"]["charges"]
    by_order: dict[str, list] = {}
    for ch in charges:
        by_order.setdefault(ch["order_id"], []).append(ch)
    dup = [o for o, chs in by_order.items() if sum(c["status"] == "succeeded" for c in chs) >= 2]
    retried = [o for o, chs in by_order.items()
               if [c["status"] for c in sorted(chs, key=lambda c: days_ago(c["created_at"]), reverse=True)]
               == ["failed", "succeeded"]]
    assert len(dup) >= 2 and len(retried) >= 1
    assert any(r["status"] == "processed" for r in data["refunds"]["refunds"])


def test_sd6_all_dates_are_relative(data: dict) -> None:
    iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    blob = json.dumps(data)
    assert not iso.search(blob), "absolute date found in seed data"
    resolved = resolve_relative({"a": "@now-29d", "b": "@now+2.5d", "c": "@now", "d": "@nowish"}, NOW)
    assert resolved["a"] == (NOW - timedelta(days=29)).isoformat()
    assert resolved["b"] == (NOW + timedelta(days=2.5)).isoformat()
    assert resolved["c"] == NOW.isoformat() and resolved["d"] == "@nowish"


def test_referential_integrity_and_totals(data: dict) -> None:
    customers = {c["customer_id"] for c in data["customers"]["customers"]}
    orders = {o["order_id"]: o for o in data["orders"]["orders"]}
    skus = {p["sku"] for p in data["catalog"]["products"]}
    assert len(orders) == len(data["orders"]["orders"])
    for o in orders.values():
        assert o["customer_id"] in customers
        assert o["subtotal"] == sum(l["qty"] * l["unit_price"] for l in o["items"])
        assert o["total"] == o["subtotal"] + o["shipping_fee"]
        assert all(l["sku"] in skus for l in o["items"])
        assert "_tags" not in o or isinstance(o["_tags"], list)
    for rec in data["payments"]["charges"] + data["returns"]["returns"] + data["refunds"]["refunds"]:
        assert rec["order_id"] in orders
    for oid in {r["order_id"] for r in data["refunds"]["refunds"]}:
        refunded = sum(r["amount"] for r in data["refunds"]["refunds"] if r["order_id"] == oid)
        assert refunded <= orders[oid]["total"], oid


def test_no_full_card_numbers_in_seed(data: dict) -> None:
    assert not re.search(r"\b\d{13,19}\b", json.dumps(data))


def test_scenarios_reference_real_records(data: dict) -> None:
    customers = {c["customer_id"]: c for c in data["customers"]["customers"]}
    orders = {o["order_id"]: o for o in data["orders"]["orders"]}
    for name, sc in data["scenarios"]["scenarios"].items():
        if "customer_id" in sc:
            assert sc["customer_id"] in customers, name
        for oid in sc.get("order_ids", []) + ([sc["order_id"]] if "order_id" in sc else []):
            assert oid in orders, name
    sc = data["scenarios"]["scenarios"]
    assert orders[sc["not_owner"]["order_id"]]["customer_id"] != sc["not_owner"]["customer_id"]
    assert sc["secondary_email_owner"]["email"] in customers[sc["secondary_email_owner"]["customer_id"]]["emails"]
    assert not any(sc["unknown_sender"]["email"] in c["emails"] for c in customers.values())
    assert not any(o["customer_id"] == sc["customer_no_orders"]["customer_id"] for o in orders.values())


def test_recent_order_scenarios_are_exact(data: dict) -> None:
    """Filler must not add recent orders to customers whose scenario depends on their order count."""
    sc = data["scenarios"]["scenarios"]
    orders = data["orders"]["orders"]

    def recent(cid: str) -> set[str]:
        return {o["order_id"] for o in orders if o["customer_id"] == cid and days_ago(o["placed_at"]) <= 60}

    assert recent(sc["single_recent_order"]["customer_id"]) == {sc["single_recent_order"]["order_id"]}
    assert recent(sc["several_recent_orders"]["customer_id"]) == set(sc["several_recent_orders"]["order_ids"])
