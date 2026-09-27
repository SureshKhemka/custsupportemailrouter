"""Labelled dataset tooling (DS-1..DS-7): loading, consistency check, and that the check catches mistakes."""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from router.config import load_config
from router.dataset.check import check_dataset
from router.dataset.loader import load_gate_cases, load_records, reference_now, render_dates, write_inbox
from router.dataset.schema import Fact, GateCase, Label
from router.schemas.email import InboundEmail

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cfg():
    return load_config(root=ROOT, use_local=False, env={}).settings


@pytest.fixture
def records(cfg):
    return load_records(cfg.paths.dataset)


def by_id(records, rid):
    return next(r for r in records if r.record.id == rid)


def record_problems(records, cfg, rid):
    return [p for p in check_dataset(records, cfg, cfg.paths.dataset).problems if p.startswith(rid)]


def test_committed_dataset_is_consistent(records, cfg) -> None:
    res = check_dataset(records, cfg, cfg.paths.dataset)
    assert res.ok, "\n".join(res.problems)
    assert res.stats["total"] >= 200 and res.stats["by_split"]["test"] > 0


def test_splits_do_not_share_records_or_groups(records) -> None:
    dev = {r.record.group_id for r in records if r.split == "dev"}
    test = {r.record.group_id for r in records if r.split == "test"}
    assert not dev & test


@pytest.mark.parametrize("rid,mutate,expected", [
    ("D062", lambda l: l.intents[0].decision.update(refund_amount=5500), "refund_amount"),
    ("D032", lambda l: l.intents[0].decision.update(eligible=False), "decision.eligible"),
    ("D005", lambda l: l.intents[0].decision.update(delivery_state="delayed"), "delivery_state"),
    ("D014", lambda l: l.escalation.clear(), "escalation 'vip'"),
    ("D085", lambda l: l.actions.clear(), "actions:"),
    ("D073", lambda l: setattr(l.case, "queue", "general"), "case.queue"),
    ("D115", lambda l: setattr(l, "ownership", "owner"), "ownership"),
    ("D096", lambda l: l.proposed_actions.clear(), "proposed_actions missing"),
    ("D175", lambda l: l.escalation.clear(), "escalation 'repeat_contact'"),
    ("D043", lambda l: l.reply.must_contain.append(Fact(kind="amount", value=8000)), "reply amount"),
    ("D094", lambda l: setattr(l.case, "mode", "ROUTE"), "case.mode"),
])
def test_check_catches_label_mistakes(records, cfg, rid, mutate, expected) -> None:
    mutate(by_id(records, rid).record.label)
    probs = record_problems(records, cfg, rid)
    assert any(expected in p for p in probs), probs


def test_check_follows_config_changes(records, tmp_path) -> None:
    """Changing a handling choice changes expectations; labels written for the base config then disagree."""
    overlay = tmp_path / "o.yaml"
    overlay.write_text("handling:\n  escalation:\n    auto_becomes: ROUTE\n")
    cfg2 = load_config([overlay], root=ROOT, use_local=False, env={}).settings
    assert any("case.mode" in p for p in record_problems(records, cfg2, "D014"))


def test_label_order_reference_validation() -> None:
    with pytest.raises(ValueError, match="order must be"):
        Label.model_validate({"intents": [{"intent": "other", "mode": "ROUTE"}], "order": "12345",
                              "case": {"mode": "ROUTE", "disposition": "routed"}})


def test_fact_shorthand() -> None:
    assert Fact.model_validate({"amount": 2499}) == Fact(kind="amount", value=2499)


def test_write_inbox_produces_valid_email_files(records, tmp_path) -> None:
    subset = [r for r in records if r.record.group_id == "G-D174"]
    paths = write_inbox(subset, tmp_path)
    emails = [InboundEmail.model_validate(json.loads(p.read_text())) for p in paths]
    assert [e.message_id for e in emails] == ["<irfan-pb-1@mail.example.com>", "<irfan-pb-2@mail.example.com>"]
    assert emails[1].in_reply_to == emails[0].message_id
    assert all(e.received_at.tzinfo is not None for e in emails)


def test_render_dates_and_gate_cases(cfg) -> None:
    tz = ZoneInfo(cfg.app.timezone)
    now = reference_now(cfg.paths.dataset)
    assert render_dates("on <<date:@now-28d>>.", now, tz) == "on 30 August 2026."
    cases = load_gate_cases(cfg.paths.dataset, tz)
    assert "<<date" not in next(c for c in cases if c.id == "B002").reply


def test_gate_case_block_needs_checks() -> None:
    with pytest.raises(ValueError, match="must name the gate checks"):
        GateCase.model_validate({"id": "B999", "record": "D001", "defect": "x", "expect": "block", "reply": "r"})
