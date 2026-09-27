"""Human review (FR-34..FR-37): queue order, approve / edit / reject / reassign, re-gating, held actions, audit."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from router.clients import Backends
from router.core.clock import FixedClock
from router.dataset.loader import load_records
from router.evals.oracle import label_oracle
from router.pipeline.runner import Router
from router.review.service import ReviewError, ReviewService
from router.store.db import Store

NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


@pytest.fixture(scope="module")
def records(pinned_config):
    return {r.record.id: r for r in load_records(pinned_config.settings.paths.dataset)}


@pytest.fixture
def env(pinned_config, mocks, records):
    store = Store(":memory:")
    router = Router(pinned_config, store, FixedClock(NOW), understander=label_oracle(list(records.values())),
                    http_clients=mocks)
    svc = ReviewService(store, Backends.from_config(pinned_config.settings, http_clients=mocks, sleep=lambda s: None),
                        pinned_config.settings)

    def case(rid: str) -> str:
        return router.process_email(records[rid].email).case_id
    return svc, case, mocks, store


def outbox(mocks):
    return list(mocks["outbox"].get("/_admin/state").json()["messages"].values())


def test_queue_order_priority_then_sla(env) -> None:
    svc, case, _, _ = env
    general, legal = case("D062"), case("D096")  # damaged draft (general), legal threat (priority)
    ids = [c["case_id"] for c in svc.list_cases(None, NOW)]
    assert ids.index(legal) < ids.index(general)
    q = {x["queue"]: x for x in svc.queues(NOW)}
    assert q["legal"]["priority"] == 1 and q["general"]["open"] >= 1
    later = NOW + timedelta(hours=5)  # past the 4h SLA
    assert all(c["sla"] == "breached" for c in svc.list_cases(None, later))


def test_approve_unchanged_runs_held_action_once_and_sends(env) -> None:
    svc, case, mocks, store = env
    cid = case("D091")  # VIP cancel: DRAFT with the cancellation held
    assert [a["status"] for a in store.case_actions(cid)] == ["proposed"]
    res = svc.approve(cid, "asha", NOW)
    assert res.sent and res.edit_class == "unchanged" and res.actions[0]["status"] == "succeeded"
    assert mocks["order"].get("/orders/ORD-100103").json()["status"] == "cancelled"
    with pytest.raises(ReviewError, match="not waiting for a human"):
        svc.approve(cid, "asha", NOW)  # double click
    assert len(outbox(mocks)) == 1
    [act] = store.agent_actions(cid)
    assert (act["action"], act["agent"], act["decision_changed"]) == ("approve", "asha", False)
    assert store.get_case(cid)["resolution"] == "human_replied"


def test_edit_is_measured(env) -> None:
    svc, case, _, store = env
    cid = case("D062")
    draft = svc.view(cid, NOW).draft["text"]
    res = svc.approve(cid, "asha", NOW, text=draft.replace("I'm sorry", "I'm really sorry"))
    assert res.sent and res.edit_class == "light" and 0 < res.edit_size <= 0.2
    assert store.drafts(cid)[-1]["status"] == "edited_and_approved"


def test_edit_with_false_claim_is_blocked(env) -> None:
    svc, case, mocks, store = env
    cid = case("D062")  # the refund is only proposed
    bad = "Hi Kunal,\n\nWe've refunded ₹5,499 to your card already.\n\nWarm regards,\nCustomer Care\n"
    res = svc.approve(cid, "asha", NOW, text=bad, run_actions=False)
    assert not res.sent and "claimed_actions_succeeded" in res.gate.failures and outbox(mocks) == []
    assert store.agent_actions(cid)[-1]["action"] == "approve_blocked_by_gate"
    assert store.get_case(cid)["status"] == "open"


def test_approving_without_actions_is_a_decision_change(env) -> None:
    svc, case, mocks, store = env
    cid = case("D091")
    text = svc.view(cid, NOW).draft["text"].replace("We can cancel order ORD-100103 for you.",
                                                    "Your order ORD-100103 is still active; tell us if you still want it cancelled.")
    res = svc.approve(cid, "asha", NOW, text=text, run_actions=False)
    assert res.sent and store.agent_actions(cid)[-1]["decision_changed"] is True
    assert mocks["order"].get("/orders/ORD-100103").json()["status"] == "placed"


def test_failed_action_on_approve_sends_nothing(env) -> None:
    svc, case, mocks, _ = env
    cid = case("D091")
    mocks["order"].put("/_admin/faults", json={"enabled": True, "scripted": [
        {"endpoint": "POST /orders/{order_id}/cancel", "fail_times": 5, "kind": "error"}]})
    res = svc.approve(cid, "asha", NOW)
    assert not res.sent and "action failed" in res.problem and outbox(mocks) == []


def test_legal_case_needs_an_authored_reply(env) -> None:
    svc, case, mocks, store = env
    cid = case("D095")
    with pytest.raises(ReviewError, match="no draft"):
        svc.approve(cid, "legal-team", NOW)
    res = svc.approve(cid, "legal-team", NOW, text="Hi Aditya,\n\nThank you for your message. Our legal team has received "
                                                    "your notice and will respond in writing.\n\nWarm regards,\nCustomer Care\n")
    assert res.sent and res.edit_class == "authored"


def test_reject_and_reassign_are_recorded(env) -> None:
    svc, case, _, store = env
    cid = case("D062")
    with pytest.raises(ReviewError, match="reason"):
        svc.reject(cid, "asha", "  ", NOW)
    svc.reject(cid, "asha", "customer called; resolved by phone", NOW, close=True)
    assert store.get_case(cid)["resolution"] == "closed_no_reply" and store.drafts(cid)[-1]["status"] == "rejected"
    cid2 = case("D073")
    svc.reassign(cid2, "asha", "general", NOW, "not a billing issue")
    assert store.get_case(cid2)["queue"] == "general"
    with pytest.raises(ReviewError, match="unknown queue"):
        svc.reassign(cid2, "asha", "nowhere", NOW)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM agent_actions")


def test_view_has_everything_the_agent_needs(env) -> None:
    svc, case, _, _ = env
    v = svc.view(case("D131"), NOW)
    assert v.understanding["intents"] == ["order_status", "billing_dispute"]
    assert v.decision["intents"] and v.facts.orders[0].order_id == "ORD-100038"
    assert v.draft is not None and v.case["queue"] == "billing"
