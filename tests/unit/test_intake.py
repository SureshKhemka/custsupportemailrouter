"""Intake (FR-2..FR-4, FR-6): duplicates, threads, near-duplicate candidates, automated mail, bad files."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from router.core.clock import FixedClock
from router.pipeline.runner import Router
from router.schemas.email import InboundEmail
from router.store.db import Store

NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")


def email(mid: str, body: str = "Where is ORD-100004?", sender: str = "kavya.menon@example.com",
          minutes_ago: float = 60, **kw) -> InboundEmail:
    return InboundEmail.model_validate({"message_id": mid, "from": {"email": sender}, "to": "support@shop.example",
                                        "subject": "order", "body": body,
                                        "received_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), **kw})


@pytest.fixture
def router(pinned_config, mocks):
    return Router(pinned_config, Store(":memory:"), FixedClock(NOW), http_clients=mocks)


def test_new_case_then_duplicate_is_ignored(router) -> None:
    a = router.process_email(email("<m1@x>"))
    b = router.process_email(email("<m1@x>", minutes_ago=59))
    assert a.stage == "identified" and b.stage == "duplicate_ignored" and b.case_id == a.case_id
    assert [e.outcome for e in router.store.case_emails(a.case_id)] == ["new_case", "duplicate"]


def test_reply_joins_thread_and_inherits_order(router) -> None:
    a = router.process_email(email("<m1@x>", minutes_ago=300))
    b = router.process_email(email("<m2@x>", body="Any update?", minutes_ago=10, in_reply_to="<m1@x>"))
    assert b.intake.outcome == "follow_up" and b.case_id == a.case_id
    assert b.identity.inherited_from_case and b.identity.primary_order_id == "ORD-100004"
    assert b.identity.contact_count == 2


def test_near_duplicate_candidate_same_sender_same_order_within_window(router) -> None:
    a = router.process_email(email("<m1@x>", minutes_ago=40))
    b = router.process_email(email("<m2@x>", body="Sending again: ORD-100004, where is it?", minutes_ago=20))
    c = router.process_email(email("<m3@x>", body="Separate question about ORD-100045", minutes_ago=10))
    assert b.intake.near_duplicate_of == a.case_id and b.case_id != a.case_id
    assert c.intake.near_duplicate_of is None


def test_no_candidate_outside_window(router) -> None:
    router.process_email(email("<m1@x>", minutes_ago=200))
    assert router.process_email(email("<m2@x>", minutes_ago=10)).intake.near_duplicate_of is None


@pytest.mark.parametrize("headers,sender", [
    ({"Auto-Submitted": "auto-replied"}, "kavya.menon@example.com"),
    ({"Precedence": "bulk"}, "news@example.com"),
    ({"List-Unsubscribe": "<mailto:u@x>"}, "news@example.com"),
    ({}, "mailer-daemon@mx.example.net"),
    ({"Content-Type": "multipart/report; report-type=delivery-status"}, "x@example.net"),
])
def test_automated_mail_is_closed_without_identity_calls(router, headers, sender) -> None:
    ctx = router.process_email(email("<a@x>", sender=sender, headers=headers))
    assert ctx.stage == "closed_automated" and ctx.identity is None
    assert router.store.get_case(ctx.case_id)["disposition"] == "closed_spam"
    assert not router.store.events(ctx.case_id, kind="backend_call")


def test_auto_submitted_no_is_a_real_email(router) -> None:
    assert router.process_email(email("<a@x>", headers={"Auto-Submitted": "no"})).stage == "identified"


def test_bad_files_do_not_stop_the_run(router, tmp_path) -> None:
    (tmp_path / "a.json").write_text("{not json")
    (tmp_path / "b.json").write_text(json.dumps({"message_id": "<x>", "body": "no sender"}))
    good = email("<ok@x>").model_dump(mode="json", by_alias=True)
    (tmp_path / "c.json").write_text(json.dumps(good))
    summary = router.process_inbox(tmp_path)
    assert summary.outcomes == {"identified": 1} and len(summary.failures) == 2
    assert len(router.store.events(kind="intake_error")) == 2


def test_backend_calls_are_audited_with_step_ids(router) -> None:
    ctx = router.process_email(email("<m1@x>"))
    calls = router.store.events(ctx.case_id, kind="backend_call")
    assert {c["data"]["service"] for c in calls} >= {"customer", "order", "refund", "returns", "payments"}
    assert all(c["step_id"].startswith(ctx.case_id + "/") for c in calls)


def test_reprocessing_same_inbox_is_safe(router, tmp_path) -> None:
    (tmp_path / "a.json").write_text(json.dumps(email("<m1@x>").model_dump(mode="json", by_alias=True)))
    router.process_inbox(tmp_path)
    second = router.process_inbox(tmp_path)
    assert second.outcomes == {"duplicate_ignored": 1}
    assert len(router.store.list_cases()) == 1


def test_step_ids_unique_across_emails_of_a_case(router) -> None:
    a = router.process_email(email("<m1@x>", minutes_ago=300))
    router.process_email(email("<m2@x>", body="Any update?", minutes_ago=10, in_reply_to="<m1@x>"))
    router.process_email(email("<m1@x>", minutes_ago=299))  # duplicate delivery
    steps = [e["step_id"] for e in router.store.events(a.case_id)]
    assert len(steps) == len(set(steps))
