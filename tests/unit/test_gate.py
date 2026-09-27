"""Outbound gate (FR-31, FR-32): each check, plus hard gate HG-5 over the DS-6 set."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from router.config import load_config
from router.dataset.check import SeedView
from router.dataset.gate_facts import facts_for_record
from router.dataset.loader import load_gate_cases, load_rated_replies, load_records, reference_now
from router.gate import ActionRecord, CaseFacts, run_gate
from router.gate.gate import detect_language
from router.schemas.backend import Customer, Order, Refund

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")
TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def cfg():
    return load_config(root=ROOT, use_local=False, env={}).settings


@pytest.fixture(scope="module")
def ds(cfg):
    recs = {r.record.id: r for r in load_records(cfg.paths.dataset)}
    seed = SeedView.load(cfg.paths.seed, reference_now(cfg.paths.dataset))
    return recs, seed


# --------------------------------------------------------------------------- HG-5


def test_hg5_gate_blocks_every_bad_reply_and_passes_controls(cfg, ds) -> None:
    recs, seed = ds
    wrong = []
    for c in load_gate_cases(cfg.paths.dataset, TZ):
        res = run_gate(c.reply, facts_for_record(recs[c.record], seed, cfg, c.recorded_actions))
        blocked = not res.passed
        if blocked != (c.expect == "block") or not set(c.checks) <= set(res.failed_checks):
            wrong.append((c.id, c.expect, res.failures))
    assert not wrong, wrong


def test_gate_does_not_block_correct_rated_replies(cfg, ds) -> None:
    """Guard against over-blocking: replies rated correct (>= 4) by the DS-7 set must pass."""
    recs, seed = ds
    blocked = [(r.id, run_gate(r.reply, facts_for_record(recs[r.record], seed, cfg)).failures)
               for r in load_rated_replies(cfg.paths.dataset) if r.ratings["correctness"] >= 4]
    assert not [b for b in blocked if b[1]], blocked


# --------------------------------------------------------------------------- unit checks on a small case


@pytest.fixture
def facts(cfg) -> CaseFacts:
    me = Customer(customer_id="C1", name="Kavya Menon", emails=["kavya@example.com"], phone="+91 9811111111", tier="standard")
    other = Customer(customer_id="C2", name="Isha Kapoor", emails=["isha@example.com"], phone="+91 9822222222", tier="standard")
    order = Order.model_validate({
        "order_id": "ORD-100004", "customer_id": "C1", "status": "delivered", "currency": "INR",
        "items": [{"line_id": "ORD-100004-L1", "sku": "S", "name": "Air Fryer 4L", "category": "home", "qty": 1, "unit_price": 6999}],
        "subtotal": 6999, "shipping_fee": 0, "total": 6999,
        "placed_at": NOW - timedelta(days=8), "shipped_at": NOW - timedelta(days=7),
        "delivered_at": NOW - timedelta(days=5), "promised_delivery_date": NOW - timedelta(days=5),
        "tracking_number": "EK8667526689IN", "tracking_events": [],
    })
    return CaseFacts(language="en", customer=me, sender_display_name="Kavya", orders=[order], policy=cfg.policy, tz=TZ,
                     now=NOW, refunds={"ORD-100004": [Refund(refund_id="R1", order_id="ORD-100004", amount=6999,
                                                              status="initiated", reason="x", created_at=NOW - timedelta(days=1))]},
                     all_customers=[me, other], order_owner={"ORD-100004": "C1", "ORD-100006": "C2"})


def fails(reply: str, facts: CaseFacts) -> list[str]:
    return run_gate(reply, facts).failed_checks


def test_clean_reply_passes(facts) -> None:
    assert fails("Hi Kavya, your air fryer (ORD-100004) was delivered on 22 September 2026. "
                 "The refund of ₹6,999 has been initiated. Tracking EK8667526689IN.", facts) == []


@pytest.mark.parametrize("reply", [
    "Hi Kavya, order ORD-100005 is on its way.",                    # not this case's order
    "Hi Kavya, tracking EK0000000000IN.",                           # wrong tracking
    "Hi Kavya, you paid ₹7,499 for ORD-100004.",                     # wrong amount
    "Hi Kavya, it was delivered on 20 September 2026.",              # wrong delivered date
    "Hi Kavya, it was delivered 3 days ago.",                        # wrong relative date
])
def test_facts_mismatch_blocks(facts, reply) -> None:
    assert "facts_match_backend" in fails(reply, facts)


def test_amount_formats(facts) -> None:
    for a in ("₹6,999", "Rs. 6999", "INR 6,999.00", "6999 rupees"):
        assert "facts_match_backend" not in fails(f"Hi Kavya, the refund of {a} has been initiated.", facts), a


def test_refund_claims_need_matching_state(facts) -> None:
    assert "claimed_actions_succeeded" in fails("Hi Kavya, your refund has been credited to your account.", facts)
    assert "claimed_actions_succeeded" not in fails("Hi Kavya, your refund has been initiated.", facts)
    assert "claimed_actions_succeeded" not in fails("Hi Kavya, once received, your refund will be processed.", facts)


def test_action_claims_need_succeeded_action(facts) -> None:
    claim = "Hi Kavya, we've created a return for ORD-100004."
    assert "claimed_actions_succeeded" in fails(claim, facts)
    facts.actions.append(ActionRecord("create_return", "ORD-100004", "failed"))
    assert "claimed_actions_succeeded" in fails(claim, facts)
    facts.actions.append(ActionRecord("create_return", "ORD-100004", "succeeded"))
    assert "claimed_actions_succeeded" not in fails(claim, facts)
    assert "claimed_actions_succeeded" in fails("Hi Kavya, your order has been cancelled.", facts)
    assert "claimed_actions_succeeded" in fails("Hi Kavya, we've shipped a replacement.", facts)
    assert "claimed_actions_succeeded" not in fails("Hi Kavya, we'd like to send you a replacement.", facts)


@pytest.mark.parametrize("reply", [
    "Hi Isha, your order is on its way.",
    "Hi Kavya, Isha Kapoor also ordered this.",
    "Hi Kavya, please contact isha@example.com.",
    "Hi Kavya, call +91 98222 22222.",
    "Hi Kavya, ORD-100006 is delayed too.",
])
def test_other_customer_data_blocks(facts, reply) -> None:
    assert "no_other_customer_data" in fails(reply, facts)


def test_payment_details_block(facts) -> None:
    assert "no_payment_details" in fails("Hi Kavya, card 4111-1111-1111-1111 was charged.", facts)
    assert "no_payment_details" in fails("Hi Kavya, please confirm the CVV 123.", facts)
    assert "no_payment_details" not in fails("Hi Kavya, the card ending 1111 was charged ₹6,999.", facts)


def test_language_check(facts) -> None:
    assert "language_matches" in fails("Hola Kavya, tu freidora de aire fue entregada. Gracias por tu compra.", facts)
    assert detect_language("Hallo, Ihre Bestellung ist unterwegs und kommt bald an.") == "de"


@pytest.mark.parametrize("ph", ["{{name}}", "{first_name}", "[DATE]", "<<date:@now>>", "<CUSTOMER_NAME>", "XXXX"])
def test_placeholders_block(facts, ph) -> None:
    assert "no_placeholders" in fails(f"Hi Kavya, your order ships on {ph}.", facts)


@pytest.mark.parametrize("reply,blocked", [
    ("Hi Kavya, returns are accepted within 60 days.", True),
    ("Hi Kavya, returns are accepted within 30 days of delivery.", False),
    ("Hi Kavya, we guarantee delivery tomorrow.", True),
    ("Hi Kavya, I can't guarantee a delivery date, but you can track it.", False),
    ("Hi Kavya, as a goodwill gesture we'll add store credit.", True),
    ("Hi Kavya, the refund will be credited by tomorrow.", True),
    ("Hi Kavya, you can keep the item.", True),
    ("Hi Kavya, it was delivered 5 days ago, within the 30-day return window.", False),
])
def test_policy_statements(facts, reply, blocked) -> None:
    assert ("consistent_with_policy" in fails(reply, facts)) is blocked


@pytest.mark.parametrize("junk", ["<built-in method items of dict object at 0x108caca80>", "{% if x %}",
                                  "order (None)", "status: None", "Undefined"])
def test_rendering_artifacts_are_blocked(facts, junk) -> None:
    assert "no_placeholders" in fails(f"Hi Kavya, your order {junk} is on its way.", facts)


@pytest.mark.parametrize("claim", [
    "Hi Kavya, we have also cancelled order ORD-100004 as requested.",
    "Hi Kavya, I've gone ahead and cancelled your order.",
    "Hi Kavya, your order is now cancelled.",
    "Hi Kavya, I have already created a return for ORD-100004.",
    "Hi Kavya, the refund has now been credited.",
    "Hi Kavya, we've successfully shipped a replacement.",
])
def test_claims_with_adverbs_or_first_person_are_checked(facts, claim) -> None:
    assert "claimed_actions_succeeded" in fails(claim, facts)


def test_policy_photo_threshold_may_be_quoted(facts) -> None:
    assert "facts_match_backend" not in fails("Hi Kavya, we need a photo for items above ₹2,000.", facts)
