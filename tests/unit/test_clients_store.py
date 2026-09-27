"""Service clients (retries, error mapping, call records) and the case store (append-only, masking)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import httpx
import pytest

from router.clients import Backends, ServiceError
from router.clients.http import ServiceClient
from router.config.models import ServiceConfig
from router.store.db import Store

NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


@pytest.fixture
def backends(pinned_config, mocks):
    calls = []
    b = Backends.from_config(pinned_config.settings, on_call=calls.append, http_clients=mocks, sleep=lambda s: None)
    return b, calls, mocks


def test_lookups_and_not_found(backends) -> None:
    b, calls, _ = backends
    assert b.customer.by_email("PRIYA.P.SHOP@example.in").customer_id == "CUST-0002"
    assert b.customer.by_email("nobody@example.net") is None
    assert b.order.get("ORD-999999") is None
    assert {o.order_id for o in b.order.by_customer("CUST-0009", 60)} == {"ORD-100044", "ORD-100045", "ORD-100046"}
    assert calls[0].service == "customer" and calls[0].status == 200 and calls[0].attempts == 1


def test_retry_after_injected_error(backends) -> None:
    b, calls, clients = backends
    clients["order"].put("/_admin/faults", json={"enabled": True, "scripted": [
        {"endpoint": "GET /orders/{order_id}", "match": {"order_id": "ORD-100004"}, "fail_times": 1, "kind": "error"}]})
    assert b.order.get("ORD-100004").order_id == "ORD-100004"
    assert calls[-1].attempts == 2 and calls[-1].error is None


def test_retries_exhausted_raise_unavailable(backends) -> None:
    b, calls, clients = backends
    clients["order"].put("/_admin/faults", json={"enabled": True, "error_rate": 1.0})
    with pytest.raises(ServiceError) as exc:
        b.order.get("ORD-100004")
    assert exc.value.kind == "unavailable" and calls[-1].attempts == 3  # 1 + 2 retries


def test_rejection_is_not_retried(backends) -> None:
    b, calls, _ = backends
    with pytest.raises(ServiceError) as exc:
        b.refund.create("ORD-100014", 100000, "x", "C-1", "k-1")
    assert exc.value.kind == "rejected" and calls[-1].attempts == 1


def test_idempotent_action_through_client(backends) -> None:
    b, _, _ = backends
    lines = [("ORD-100015-L1", 1)]
    first = b.returns.create("ORD-100015", lines, "size", "C-1", "C-1:create_return:ORD-100015:x")
    again = b.returns.create("ORD-100015", lines, "size", "C-1", "C-1:create_return:ORD-100015:x")
    assert first["return_id"] == again["return_id"]


def test_timeout_is_retried() -> None:
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    cfg = ServiceConfig(base_url="http://svc", timeout_s=1, retries=2, backoff_s=0)
    c = ServiceClient("svc", cfg, client=httpx.Client(base_url="http://svc", transport=httpx.MockTransport(handler)),
                      sleep=lambda s: None)
    assert c.request("GET", "/x", op="x") == {"ok": True} and n["calls"] == 2


# --------------------------------------------------------------------------- store


def test_events_are_append_only_and_masked() -> None:
    s = Store(":memory:")
    seq = s.append_event("C-1", "C-1/001-x", "note", {"text": "card 4111 1111 1111 1111"}, NOW)
    assert s.events("C-1")[0]["data"]["text"] == "card ****-****-****-1111"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._conn.execute("UPDATE events SET kind='x' WHERE seq=?", (seq,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s._conn.execute("DELETE FROM events")


def test_cases_and_emails() -> None:
    s = Store(":memory:")
    s.create_case("C-1", thread_root="m1", sender="a@x.com", at=NOW)
    s.update_case("C-1", NOW, stage="identified", intents=["order_status"])
    assert s.get_case("C-1")["intents"] == ["order_status"]
    with pytest.raises(ValueError):
        s.update_case("C-1", NOW, nonsense=1)
    s.record_email(message_id="m1", case_id="C-1", sender="a@x.com", received_at=NOW, subject="s", source=None,
                   order_ids=["ORD-1"], outcome="new_case", at=NOW)
    assert s.first_email("m1").order_ids == ["ORD-1"] and s.first_email("m2") is None
