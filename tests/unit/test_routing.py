"""Case handling decisions (FR-10, FR-11, FR-16..FR-20) and their configurable behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.decide.routing import CaseInput, IntentInput, decide_case

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cfg():
    return load_config(root=ROOT, use_local=False, env={}).settings


def with_overlay(tmp_path: Path, data: dict):
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_config([p], root=ROOT, use_local=False, env={}).settings


def case(*intents, **kw) -> CaseInput:
    return CaseInput(intents=tuple(IntentInput(i) if isinstance(i, str) else IntentInput(i[0], frozenset(i[1:]))
                                   for i in intents), **kw)


def test_simple_auto(cfg) -> None:
    h = decide_case(case("order_status"), cfg)
    assert (h.mode, h.disposition, h.queue) == ("AUTO", "auto_replied", None)


def test_conditions_change_mode(cfg) -> None:
    assert decide_case(case(("order_status", "lost_shipment")), cfg).mode == "ROUTE"
    assert decide_case(case(("return_request", "not_eligible")), cfg).mode == "DRAFT"
    assert decide_case(case(("cancel_order", "already_shipped")), cfg).mode == "DRAFT"


def test_strictest_wins_and_queue_priority(cfg) -> None:
    h = decide_case(case("order_status", "billing_dispute"), cfg)
    assert (h.mode, h.queue) == ("ROUTE", "billing")
    h = decide_case(case("order_status", "billing_dispute", "legal_threat"), cfg)
    assert (h.mode, h.queue, h.priority) == ("ROUTE_NO_DRAFT", "legal", True)


def test_multi_intent_holds_actions_by_default(cfg) -> None:
    h = decide_case(case("return_request", "payment_issue"), cfg)
    assert h.mode == "ROUTE" and h.run_actions_for == () and h.hold_actions_for == ("return_request",)


def test_multi_intent_can_run_non_monetary_actions(tmp_path) -> None:
    cfg = with_overlay(tmp_path, {"handling": {"multi_intent": {"per_intent": {"return_request": "run"}}}})
    h = decide_case(case("return_request", "payment_issue"), cfg)
    assert h.run_actions_for == ("return_request",)


def test_escalation_default_draft_and_configurable(cfg, tmp_path) -> None:
    h = decide_case(case("order_status", signals=frozenset({"vip"})), cfg)
    assert (h.mode, h.escalated, h.queue, h.run_actions_for) == ("DRAFT", True, "general", ())
    cfg2 = with_overlay(tmp_path, {"handling": {"escalation": {"per_intent": {"order_status": "ROUTE"}}}})
    assert decide_case(case("order_status", signals=frozenset({"vip"})), cfg2).mode == "ROUTE"
    # escalation never relaxes a stricter mode
    assert decide_case(case("legal_threat", signals=frozenset({"anger"})), cfg).mode == "ROUTE_NO_DRAFT"


def test_escalated_actions_wait_even_when_run_configured(tmp_path) -> None:
    cfg = with_overlay(tmp_path, {"handling": {"multi_intent": {"actions_when_case_not_auto": "run"}}})
    h = decide_case(case("return_request", signals=frozenset({"anger"})), cfg)
    assert h.run_actions_for == () and h.hold_actions_for == ("return_request",)


def test_injection_escalates_or_only_flags(cfg, tmp_path) -> None:
    h = decide_case(case("order_status", injection=True), cfg)
    assert h.mode == "DRAFT" and "prompt_injection" in h.flags
    cfg2 = with_overlay(tmp_path, {"handling": {"injection": {"on_detect": "flag_only"}}})
    h = decide_case(case("order_status", injection=True), cfg2)
    assert h.mode == "AUTO" and "prompt_injection" in h.flags


@pytest.mark.parametrize("ownership", ["not_owner", "unknown_sender"])
def test_unverified_sender_gets_identity_reply_and_no_actions(cfg, ownership) -> None:
    h = decide_case(case("return_request", ownership=ownership), cfg)
    assert h.disposition == "identity_reply" and h.run_actions_for == () and ownership in h.flags


def test_unverified_sender_can_be_routed(tmp_path) -> None:
    cfg = with_overlay(tmp_path, {"handling": {"identity": {"on_unverified": "route"}}})
    assert decide_case(case("order_status", ownership="unknown_sender"), cfg).disposition == "routed"


def test_unverified_sender_with_legal_threat_goes_to_legal(cfg) -> None:
    h = decide_case(case("order_status", "legal_threat", ownership="unknown_sender"), cfg)
    assert (h.disposition, h.queue) == ("routed", "legal")


def test_unknown_sender_product_question_is_just_routed(cfg) -> None:
    h = decide_case(case("product_question", ownership="unknown_sender"), cfg)
    assert h.disposition == "routed" and "unknown_sender" not in h.flags


def test_ambiguous_order_clarifies_or_routes(cfg, tmp_path) -> None:
    assert decide_case(case("order_status", order_resolution="ambiguous"), cfg).disposition == "clarification_requested"
    cfg2 = with_overlay(tmp_path, {"handling": {"ambiguous_order": {"on_ambiguous": "route"}}})
    assert decide_case(case("order_status", order_resolution="ambiguous"), cfg2).disposition == "routed"
    # escalated ambiguous case goes to a human draft instead of an automatic question
    assert decide_case(case("order_status", order_resolution="ambiguous", signals=frozenset({"anger"})),
                       cfg).disposition == "drafted"


def test_no_matching_order_routes(cfg) -> None:
    h = decide_case(case("order_status", order_resolution="none"), cfg)
    assert h.disposition == "routed" and "no_matching_order" in h.flags


def test_unsupported_language_routes_but_keeps_strictest_queue(cfg) -> None:
    h = decide_case(case("order_status", language_supported=False), cfg)
    assert (h.mode, h.queue) == ("ROUTE", "general")
    assert decide_case(case("legal_threat", language_supported=False), cfg).queue == "legal"


def test_spam_duplicates_and_merges(cfg) -> None:
    assert decide_case(case("spam_or_auto"), cfg).disposition == "closed_spam"
    assert decide_case(case("spam_or_auto", "order_status"), cfg).disposition == "auto_replied"
    assert decide_case(case("order_status", duplicate=True), cfg).disposition == "duplicate_ignored"
    assert decide_case(case("order_status", merged=True), cfg).disposition == "merged"


def test_never_auto_intents_stay_human_under_any_handling_config(tmp_path) -> None:
    cfg = with_overlay(tmp_path, {"handling": {"injection": {"on_detect": "flag_only"},
                                               "multi_intent": {"actions_when_case_not_auto": "run"}}})
    for intent in ["billing_dispute", "payment_issue", "legal_threat", "abuse"]:
        assert decide_case(case(intent), cfg).mode in {"ROUTE", "ROUTE_NO_DRAFT"}
