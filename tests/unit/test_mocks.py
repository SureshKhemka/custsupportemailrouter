"""Mock services: behaviour, idempotency, call log, reset, clock, faults, persistence (MS-1..MS-9)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mocks.app import build_app, build_service
from mocks.base import create_app
from router.config import load_config

ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-09-27T10:00:00+05:30"
NOW_UTC = datetime.fromisoformat(NOW)
SCEN = json.loads((ROOT / "seed" / "scenarios.json").read_text())["scenarios"]


@pytest.fixture
def cfg(tmp_path: Path):
    overlay = tmp_path / "test.yaml"
    overlay.write_text(yaml.safe_dump({
        "clock": {"fixed_now": NOW},
        "faults": {"timeout_hang_s": 0.01},
        "paths": {"outbox": str(tmp_path / "outbox"), "mock_state": str(tmp_path / "state")},
    }))
    return load_config([overlay], root=ROOT, use_local=False, env={})


def client(cfg, name: str, *, persist: bool = False) -> TestClient:
    return TestClient(build_app(name, cfg, persist=persist))


def scripted(c: TestClient, *rules: dict, **rates) -> None:
    r = c.put("/_admin/faults", json={"enabled": True, "scripted": list(rules), **rates})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- customer / order / payments


def test_customer_lookup_by_any_registered_email_case_insensitive(cfg) -> None:
    c = client(cfg, "customer")
    sc = SCEN["secondary_email_owner"]
    r = c.get("/customers/by-email", params={"email": "  " + sc["email"].upper()})
    assert r.status_code == 200 and r.json()["customer_id"] == sc["customer_id"]
    r = c.get("/customers/by-email", params={"email": SCEN["unknown_sender"]["email"]})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "customer_not_found"
    assert c.get(f"/customers/{SCEN['repeat_contact_third']['customer_id']}/contacts").json()


def test_seed_dates_resolve_against_pinned_now(cfg) -> None:
    c = client(cfg, "order")
    o = c.get(f"/orders/{SCEN['return_window_day_30']['order_id']}").json()
    assert datetime.fromisoformat(o["delivered_at"]) == NOW_UTC - timedelta(days=30)
    assert "_tags" not in c.get(f"/orders/{SCEN['status_lost']['order_id']}").json()


def test_orders_by_customer_and_recency(cfg) -> None:
    c = client(cfg, "order")
    sc = SCEN["several_recent_orders"]
    recent = c.get(f"/customers/{sc['customer_id']}/orders", params={"placed_within_days": 60}).json()
    assert {o["order_id"] for o in recent} == set(sc["order_ids"])
    placed = [o["placed_at"] for o in recent]
    assert placed == sorted(placed, reverse=True)
    assert c.get("/products/SKU-HM-003").json()["in_stock"] is False
    assert c.get("/orders/ORD-999999").status_code == 404


def test_payments_include_duplicate_charges(cfg) -> None:
    c = client(cfg, "payments")
    p = c.get(f"/orders/{SCEN['duplicate_charge']['order_id']}/payments").json()
    assert sum(ch["status"] == "succeeded" for ch in p["charges"]) == 2


# --------------------------------------------------------------------------- idempotent actions


def test_return_is_idempotent(cfg) -> None:
    c = client(cfg, "returns")
    oid = SCEN["return_window_day_29"]["order_id"]
    body = {"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "size_issue"}
    first = c.post("/returns", json=body, headers={"Idempotency-Key": "case-1:return"})
    again = c.post("/returns", json=body, headers={"Idempotency-Key": "case-1:return"})
    assert first.status_code == 201 and again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json() == again.json()
    assert len(c.get("/returns", params={"order_id": oid}).json()) == 1
    # a new key for the same line must fail: nothing left to return
    r = c.post("/returns", json=body, headers={"Idempotency-Key": "case-2:return"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "quantity_exceeds_returnable"


def test_idempotency_key_reuse_with_different_payload_is_rejected(cfg) -> None:
    c = client(cfg, "refund")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    h = {"Idempotency-Key": "case-9:refund"}
    assert c.post("/refunds", json={"order_id": oid, "amount": 100, "reason": "damaged"}, headers=h).status_code == 201
    r = c.post("/refunds", json={"order_id": oid, "amount": 200, "reason": "damaged"}, headers=h)
    assert r.status_code == 409


def test_missing_idempotency_key_is_rejected(cfg) -> None:
    c = client(cfg, "refund")
    r = c.post("/refunds", json={"order_id": SCEN["damaged_out_of_stock"]["order_id"], "amount": 1, "reason": "x"})
    assert r.status_code == 400


def test_validation_failures_are_not_remembered(cfg) -> None:
    c = client(cfg, "refund")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    h = {"Idempotency-Key": "k"}
    assert c.post("/refunds", json={"order_id": "ORD-0", "amount": 1, "reason": "x"}, headers=h).status_code == 404
    assert c.post("/refunds", json={"order_id": oid, "amount": 1, "reason": "x"}, headers=h).status_code == 201


def test_refund_never_exceeds_amount_paid(cfg) -> None:
    c = client(cfg, "refund")
    sc = SCEN["status_refunded"]  # already fully refunded (item price)
    r = c.post("/refunds", json={"order_id": sc["order_id"], "amount": 100000, "reason": "x"},
               headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "exceeds_refundable"


def test_concurrent_duplicate_refunds_create_exactly_one(cfg) -> None:
    c = client(cfg, "refund")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    body = {"order_id": oid, "amount": 5499, "reason": "damaged"}
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: c.post("/refunds", json=body, headers={"Idempotency-Key": "same"}).status_code,
                              range(16)))
    assert codes.count(201) == 1 and codes.count(200) == 15
    assert len([r for r in c.get("/refunds", params={"order_id": oid}).json() if r["idempotency_key"] == "same"]) == 1


def test_replacement_rejects_out_of_stock(cfg) -> None:
    c = client(cfg, "replacement")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    r = c.post("/replacements", json={"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "damaged"},
               headers={"Idempotency-Key": "k"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "out_of_stock"
    oid = SCEN["damaged_high_value_in_stock"]["order_id"]
    r = c.post("/replacements", json={"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "damaged"},
               headers={"Idempotency-Key": "k2"})
    assert r.status_code == 201 and r.json()["replacement_id"] == "RPL-000001"


def test_outbox_stores_messages_on_disk_and_reset_clears_them(cfg) -> None:
    c = client(cfg, "outbox")
    msg = {"case_id": "C-1", "to": "a@example.com", "subject": "Re: order", "body": "Hello", "language": "en"}
    r = c.post("/messages", json=msg, headers={"Idempotency-Key": "C-1:reply"})
    assert r.status_code == 201
    c.post("/messages", json=msg, headers={"Idempotency-Key": "C-1:reply"})
    out = cfg.settings.paths.outbox
    assert [p.name for p in out.glob("*.json")] == ["OUT-000001.json"]
    assert len(c.get("/messages", params={"case_id": "C-1"}).json()) == 1
    c.post("/_admin/reset")
    assert list(out.glob("*.json")) == [] and c.get("/messages").json() == []


# --------------------------------------------------------------------------- call log / reset / clock


def test_call_log_records_calls_but_not_admin(cfg) -> None:
    c = client(cfg, "order")
    c.get("/_admin/health")
    c.get(f"/orders/{SCEN['status_placed']['order_id']}")
    c.get("/orders/ORD-0")
    calls = c.get("/_admin/calls").json()
    assert [(x["method"], x["status"]) for x in calls] == [("GET", 200), ("GET", 404)]
    assert calls[0]["response"]["order_id"] == SCEN["status_placed"]["order_id"]
    assert c.get("/_admin/calls", params={"since": 1}).json()[0]["seq"] == 2


def test_reset_restores_seed_and_clears_calls(cfg) -> None:
    c = client(cfg, "returns")
    oid = SCEN["return_window_day_29"]["order_id"]
    body = {"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "x"}
    c.post("/returns", json=body, headers={"Idempotency-Key": "k"})
    c.post("/_admin/reset")
    assert c.get("/returns", params={"order_id": oid}).json() == []
    assert c.get("/_admin/calls").json()[-1]["path"] == "/returns"  # the GET just made
    # after reset the same key is fresh again and ids restart
    assert c.post("/returns", json=body, headers={"Idempotency-Key": "k"}).json()["return_id"] == "RA-000001"


def test_set_now_moves_clock_without_reseeding_and_reset_with_now_reseeds(cfg) -> None:
    c = client(cfg, "order")
    oid = SCEN["return_window_day_30"]["order_id"]
    delivered = c.get(f"/orders/{oid}").json()["delivered_at"]
    later = (NOW_UTC + timedelta(days=5)).isoformat()
    h = c.put("/_admin/now", json={"now": later}).json()
    assert datetime.fromisoformat(h["now"]) == NOW_UTC + timedelta(days=5)
    assert c.get(f"/orders/{oid}").json()["delivered_at"] == delivered
    c.post("/_admin/reset", json={"now": later})
    assert datetime.fromisoformat(c.get(f"/orders/{oid}").json()["delivered_at"]) == \
        NOW_UTC + timedelta(days=5) - timedelta(days=30)
    assert c.put("/_admin/now", json={"now": "2026-01-01T00:00:00"}).status_code == 422


# --------------------------------------------------------------------------- faults (MS-9)


def test_scripted_error_fails_n_times_and_does_nothing(cfg) -> None:
    c = client(cfg, "refund")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    scripted(c, {"endpoint": "POST /refunds", "match": {"order_id": oid}, "fail_times": 1, "kind": "error"})
    body = {"order_id": oid, "amount": 100, "reason": "damaged"}
    assert c.post("/refunds", json=body, headers={"Idempotency-Key": "k"}).status_code == 503
    assert [r for r in c.get("/refunds", params={"order_id": oid}).json() if r["idempotency_key"] == "k"] == []
    assert c.post("/refunds", json=body, headers={"Idempotency-Key": "k"}).status_code == 201
    faults = [x["fault"] for x in c.get("/_admin/calls").json()]
    assert faults[:2] == ["error", None]


def test_scripted_fault_only_matches_its_target(cfg) -> None:
    c = client(cfg, "order")
    target, other = SCEN["status_placed"]["order_id"], SCEN["status_packed"]["order_id"]
    scripted(c, {"endpoint": "GET /orders/{order_id}", "match": {"order_id": target}, "fail_times": 2, "kind": "error"})
    assert c.get(f"/orders/{other}").status_code == 200
    assert [c.get(f"/orders/{target}").status_code for _ in range(3)] == [503, 503, 200]


def test_timeout_after_commit_then_retry_returns_same_refund(cfg) -> None:
    """The HG-4 trap: the refund happened but the caller timed out; a retry must not refund twice."""
    c = client(cfg, "refund")
    oid = SCEN["damaged_out_of_stock"]["order_id"]
    scripted(c, {"endpoint": "POST /refunds", "fail_times": 1, "kind": "timeout_after_commit"})
    body = {"order_id": oid, "amount": 5499, "reason": "damaged"}
    first = c.post("/refunds", json=body, headers={"Idempotency-Key": "case-7:refund"})
    retry = c.post("/refunds", json=body, headers={"Idempotency-Key": "case-7:refund"})
    assert first.status_code == 504  # the caller never learns it succeeded
    assert retry.status_code == 200 and retry.headers["Idempotent-Replayed"] == "true"
    assert sum(r["idempotency_key"] == "case-7:refund" for r in c.get("/refunds", params={"order_id": oid}).json()) == 1


def test_timeout_fault_does_nothing(cfg) -> None:
    c = client(cfg, "returns")
    oid = SCEN["return_window_day_1"]["order_id"]
    scripted(c, {"endpoint": "POST /returns", "fail_times": 1, "kind": "timeout"})
    body = {"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "x"}
    assert c.post("/returns", json=body, headers={"Idempotency-Key": "k"}).status_code == 504
    assert c.get("/returns", params={"order_id": oid}).json() == []


def test_random_error_rate_is_deterministic_per_seed(cfg) -> None:
    def run() -> list[int]:
        c = client(cfg, "customer")
        scripted(c, error_rate=0.5)
        return [c.get("/customers/CUST-0001").status_code for _ in range(20)]

    a, b = run(), run()
    assert a == b and 503 in a and 200 in a


def test_faults_from_config_and_invalid_runtime_faults(tmp_path: Path) -> None:
    overlay = tmp_path / "f.yaml"
    overlay.write_text(yaml.safe_dump({"clock": {"fixed_now": NOW}, "faults": {
        "enabled": True, "services": {"customer": {"error_rate": 1.0}}}}))
    cfg = load_config([overlay], root=ROOT, use_local=False, env={})
    c = client(cfg, "customer")
    assert c.get("/customers/CUST-0001").status_code == 503
    assert c.get("/_admin/health").status_code == 200  # admin is never faulted
    assert c.put("/_admin/faults", json={"error_rate": 7}).status_code == 422


# --------------------------------------------------------------------------- persistence


def test_state_and_idempotency_survive_restart(cfg) -> None:
    oid = SCEN["return_window_day_29"]["order_id"]
    body = {"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "x"}
    c1 = client(cfg, "returns", persist=True)
    rid = c1.post("/returns", json=body, headers={"Idempotency-Key": "k"}).json()["return_id"]

    c2 = client(cfg, "returns", persist=True)  # "restart"
    again = c2.post("/returns", json=body, headers={"Idempotency-Key": "k"})
    assert again.status_code == 200 and again.json()["return_id"] == rid
    assert len(c2.get("/_admin/calls").json()) == 2  # call log persisted across restart


def test_persisted_state_discarded_when_pinned_now_changes(cfg, tmp_path: Path) -> None:
    oid = SCEN["return_window_day_29"]["order_id"]
    body = {"order_id": oid, "items": [{"line_id": f"{oid}-L1", "qty": 1}], "reason": "x"}
    client(cfg, "returns", persist=True).post("/returns", json=body, headers={"Idempotency-Key": "k"})
    svc = build_service("returns", cfg)
    svc.clock.set(NOW_UTC + timedelta(days=1))
    fresh = TestClient(create_app(type(svc)(spec=svc.spec, seed_dir=svc.seed_dir, clock=svc.clock,
                                            faults=svc.faults, state_dir=svc.state_dir)))
    assert fresh.get("/returns", params={"order_id": oid}).json() == []


def test_cancel_order_is_idempotent_and_refuses_shipped_orders(cfg) -> None:
    c = client(cfg, "order")
    oid = SCEN["cancel_before_ship"]["order_id"]
    h = {"Idempotency-Key": "case-3:cancel"}
    first = c.post(f"/orders/{oid}/cancel", json={"reason": "customer_request"}, headers=h)
    again = c.post(f"/orders/{oid}/cancel", json={"reason": "customer_request"}, headers=h)
    assert first.status_code == 201 and again.status_code == 200 and first.json() == again.json()
    assert c.get(f"/orders/{oid}").json()["status"] == "cancelled"
    shipped = SCEN["cancel_after_ship"]["order_id"]
    r = c.post(f"/orders/{shipped}/cancel", json={"reason": "x"}, headers={"Idempotency-Key": "k"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_cancellable"
