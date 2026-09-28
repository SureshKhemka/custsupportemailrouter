"""Business metrics from the case store (BM-1..BM-8) and the simulated agent's effects."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from router.clients import Backends
from router.core.clock import FixedClock
from router.core.ids import normalize_message_id
from router.dataset.loader import load_records
from router.evals.agent_sim import act_on_cases
from router.evals.oracle import label_oracle
from router.metrics.business import business_metrics, collect
from router.pipeline.runner import Router
from router.review.service import ReviewService
from router.store.db import Store

NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


@pytest.fixture(scope="module")
def records(pinned_config):
    return {r.record.id: r for r in load_records(pinned_config.settings.paths.dataset)}


def run(pinned_config, mocks, records, rids, agent=False):
    store = Store(":memory:")
    router = Router(pinned_config, store, FixedClock(NOW), understander=label_oracle(list(records.values())),
                    http_clients=mocks)
    for rid in rids:
        router.process_email(records[rid].email)
    if agent:
        svc = ReviewService(store, Backends.from_config(pinned_config.settings, http_clients=mocks, sleep=lambda s: None),
                            pinned_config.settings)
        act_on_cases(svc, store, {normalize_message_id(records[r].email.message_id): records[r] for r in rids}, NOW)
    return business_metrics(collect(store), pinned_config.settings, NOW + timedelta(days=1)), store


def test_automation_and_routing_counts(pinned_config, mocks, records) -> None:
    bm, _ = run(pinned_config, mocks, records, ["D001", "D031", "D062", "D073", "D125"])
    a = bm["BM-1_automation"]
    assert a["by_category"]["order_status"]["auto_sent"] == 1 and a["by_category"]["billing_dispute"]["to_human"] == 1
    assert a["overall_rate_excl_spam"] == 0.5  # 2 of 4 non-spam cases auto-sent
    assert bm["BM-6_gate"]["blocks"] == 0 and bm["BM-2_draft_acceptance"]["reviewed"] == 0


def test_sla_breaches_without_an_agent_and_compliance_with_one(pinned_config, mocks, records) -> None:
    bm, _ = run(pinned_config, mocks, records, ["D062"])
    assert bm["BM-5_sla"]["by_category"]["damaged_item"]["breached"] == 1
    bm, store = run(pinned_config, mocks, records, ["D062"], agent=True)
    assert bm["BM-5_sla"]["by_category"]["damaged_item"]["met"] == 1
    assert bm["BM-2_draft_acceptance"]["outcomes"] == {"unchanged": 1} and bm["BM-3_override_rate"] == 0.0


def test_recontact_is_counted_for_follow_ups(pinned_config, mocks, records) -> None:
    bm, _ = run(pinned_config, mocks, records, ["D174", "D175"])  # auto reply, then "still no update"
    assert bm["BM-4_recontact_rate"] == 1.0


def test_simulated_agent_skips_forbidden_actions_as_override(pinned_config, mocks, records) -> None:
    """D096 holds an eligible return in a legal case; with a label that forbids the return, the simulated
    agent approves without running it, which is recorded as a decision change (BM-3)."""
    strict = records["D096"].record.model_copy(deep=True)
    strict.label.proposed_actions = []
    records = {**records, "D096": type(records["D096"])(strict, records["D096"].split, records["D096"].source,
                                                        records["D096"].email)}
    bm, store = run(pinned_config, mocks, records, ["D096"], agent=True)
    assert bm["BM-3_override_rate"] == 1.0
    [case] = store.list_cases()
    [act] = store.agent_actions(case["case_id"])
    assert act["action"] == "approve" and act["edit_class"] == "authored" and act["decision_changed"]
    assert all(a["status"] != "succeeded" for a in store.case_actions(case["case_id"]))


def test_processing_time_is_recorded(pinned_config, mocks, records) -> None:
    _, store = run(pinned_config, mocks, records, ["D001"])
    [ev] = store.events(kind="processed")
    assert ev["data"]["duration_ms"] > 0
