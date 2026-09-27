"""Decide -> act -> reply -> gate -> send, against in-process mocks with label-oracle understanding.

Covers FR-18..FR-26, FR-31..FR-33, FR-22 operating modes, HG-2, HG-4 and NF-5 on real dataset emails.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.core.clock import FixedClock
from router.dataset.loader import load_records
from router.evals.oracle import label_oracle
from router.pipeline.reply import ComposedReply, inr
from router.pipeline.runner import Router
from router.store.db import Store

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


@pytest.fixture(scope="module")
def records(pinned_config):
    return {r.record.id: r for r in load_records(pinned_config.settings.paths.dataset)}


@pytest.fixture
def run(pinned_config, mocks, records):
    """Process dataset emails through the full pipeline; returns (router, contexts)."""
    def _run(*rids: str, loaded=None):
        loaded = loaded or pinned_config
        router = Router(loaded, Store(":memory:"), FixedClock(NOW), understander=label_oracle(list(records.values())),
                        http_clients=mocks)
        return router, [router.process_email(records[r].email) for r in rids]
    return _run


def outbox(mocks):
    return list(mocks["outbox"].get("/_admin/state").json()["messages"].values())


def posts(mocks, service, path_prefix):
    return [c for c in mocks[service].get("/_admin/calls").json()
            if c["method"] == "POST" and c["path"].startswith(path_prefix)]


def test_inr_formatting() -> None:
    assert [inr(x) for x in (499, 1999, 24999, 123456.5, 5499)] == \
        ["₹499", "₹1,999", "₹24,999", "₹1,23,456.50", "₹5,499"]


def test_auto_return_is_created_once_and_replied_once(run, mocks) -> None:
    router, [ctx] = run("D031")
    assert ctx.outcome.disposition == "auto_replied" and ctx.outcome.sent
    msgs = outbox(mocks)
    assert len(msgs) == 1 and "We've created a return" in msgs[0]["body"] and "Harsh" in msgs[0]["body"]
    assert router.store.case_actions(ctx.case_id)[0]["status"] == "succeeded"
    again = router.process_email(ctx.email)  # replayed delivery
    assert again.stage == "duplicate_ignored" and len(outbox(mocks)) == 1
    assert sum(c["status"] == 201 for c in posts(mocks, "returns", "/returns")) == 1


def test_failed_action_goes_to_a_human_and_is_not_claimed(run, mocks) -> None:
    mocks["returns"].put("/_admin/faults", json={"enabled": True, "scripted": [
        {"endpoint": "POST /returns", "fail_times": 3, "kind": "error"}]})
    router, [ctx] = run("D031")
    assert (ctx.outcome.disposition, ctx.outcome.queue) == ("routed", "general") and not ctx.outcome.sent
    assert "action_failed" in ctx.outcome.flags and outbox(mocks) == []
    draft = router.store.drafts(ctx.case_id)[-1]["text"]
    assert "We've created" not in draft and "couldn't set up the return" in draft


def test_timeout_after_commit_is_retried_without_duplicating(run, mocks) -> None:
    mocks["returns"].put("/_admin/faults", json={"enabled": True, "scripted": [
        {"endpoint": "POST /returns", "fail_times": 1, "kind": "timeout_after_commit"}]})
    _, [ctx] = run("D031")
    assert ctx.outcome.disposition == "auto_replied"
    returns = mocks["returns"].get("/_admin/state").json()["returns"]
    assert sum(r["order_id"] == "ORD-100015" and r["status"] == "authorised" for r in returns.values()) == 1


def test_gate_block_routes_to_gate_failures(run, mocks, monkeypatch) -> None:
    from router.pipeline import reply as reply_mod

    monkeypatch.setattr(reply_mod.Composer, "compose",
                        lambda self, *a, **k: ComposedReply("Hi Kavya,\nYour refund of ₹9,999 has been issued.\n", "auto"))
    _, [ctx] = run("D001")
    assert (ctx.outcome.disposition, ctx.outcome.queue) == ("routed", "gate_failures")
    assert set(ctx.outcome.gate.failed_checks) >= {"facts_match_backend", "claimed_actions_succeeded"}
    assert outbox(mocks) == []


def test_legal_case_has_no_draft_and_no_reply(run, mocks) -> None:
    router, [ctx] = run("D096")
    assert (ctx.outcome.mode, ctx.outcome.queue, ctx.outcome.priority) == ("ROUTE_NO_DRAFT", "legal", True)
    assert router.store.drafts(ctx.case_id) == [] and outbox(mocks) == [] and posts(mocks, "returns", "/returns") == []


def test_draft_case_stores_gated_draft(run, mocks) -> None:
    router, [ctx] = run("D062")  # damaged, refund proposed
    draft = router.store.drafts(ctx.case_id)[-1]
    assert ctx.outcome.disposition == "drafted" and draft["status"] == "pending_review" and draft["gate"] == {}
    assert "₹5,499" in draft["text"] and outbox(mocks) == []
    assert [a["status"] for a in router.store.case_actions(ctx.case_id)] == ["proposed"]
    assert posts(mocks, "refund", "/refunds") == []


@pytest.mark.parametrize("mode,disposition", [("shadow", "auto_replied"), ("draft_only", "drafted")])
def test_operating_modes_send_nothing_and_run_no_actions(pinned_config, run, mocks, tmp_path, mode, disposition) -> None:
    o = tmp_path / "o.yaml"
    o.write_text(yaml.safe_dump({"routing": {"operating_mode": {"default": mode}}}))
    loaded = load_config([*[ROOT / "config" / "eval" / "e2e.yaml"], o], root=ROOT, use_local=False, env={})
    router, [ctx] = run("D031", loaded=loaded)
    assert outbox(mocks) == [] and posts(mocks, "returns", "/returns") == []
    assert ctx.outcome.disposition == disposition
    assert router.store.drafts(ctx.case_id)[-1]["status"] == ("shadow" if mode == "shadow" else "pending_review")


def test_near_duplicate_merges_only_with_same_intents(run, mocks) -> None:
    router, [a, b] = run("D172", "D173")
    assert b.stage == "merged" and len(outbox(mocks)) == 1
    assert router.store.get_case(b.case_id)["flags"] == [f"merged_into:{a.case_id}"]


def test_identity_reply_discloses_nothing(run, mocks) -> None:
    _, [ctx] = run("D115")
    [msg] = outbox(mocks)
    assert ctx.outcome.disposition == "identity_reply" and "ORD-" not in msg["body"] and "DT3756638463IN" not in msg["body"]


def test_clarifying_question_lists_only_own_orders(run, mocks) -> None:
    _, [ctx] = run("D011")
    [msg] = outbox(mocks)
    assert ctx.outcome.disposition == "clarification_requested"
    assert {"ORD-100044", "ORD-100045", "ORD-100046"} <= set(__import__("re").findall(r"ORD-\d+", msg["body"]))
