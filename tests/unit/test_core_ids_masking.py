"""Idempotency keys (FR-24, NF-5) and payment masking (FR-41)."""

from __future__ import annotations

from router.core.ids import action_key, case_id_for, normalize_message_id, step_id
from router.core.masking import find_card_numbers, has_payment_details, luhn_ok, mask_obj, mask_text


def test_case_id_is_stable_and_normalised() -> None:
    assert case_id_for("<ABC@mail.example.com>") == case_id_for("abc@mail.example.com ")
    assert case_id_for("<a@x>") != case_id_for("<b@x>")
    assert normalize_message_id(" <X@Y> ") == "x@y"


def test_action_key_same_for_retries_different_for_different_actions() -> None:
    k = action_key("C-1", "create_return", "ORD-1", [("L1", 1)])
    assert k == action_key("C-1", "create_return", "ORD-1", [("L1", 1)])
    assert k != action_key("C-1", "create_return", "ORD-1", [("L2", 1)])
    assert k != action_key("C-2", "create_return", "ORD-1", [("L1", 1)])
    assert action_key("C-1", "issue_refund", "ORD-1", amount=5499) == action_key("C-1", "issue_refund", "ORD-1", amount=5499.0)
    assert action_key("C-1", "issue_refund", "ORD-1", amount=5499) != action_key("C-1", "issue_refund", "ORD-1", amount=5500)
    assert action_key("C-1", "x", "O", [("L2", 1), ("L1", 2)]) == action_key("C-1", "x", "O", [("L1", 2), ("L2", 1)])


def test_step_id_shares_case_id() -> None:
    assert step_id("C-abc", 3, "understand") == "C-abc/003-understand"


def test_luhn_and_card_detection() -> None:
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert find_card_numbers("card 4111 1111 1111 1111 used") == ["4111 1111 1111 1111"]
    assert find_card_numbers("order ORD-100004, tracking EK8667526689IN, phone 9811111111") == []
    assert has_payment_details("CVV: 123")


def test_masking() -> None:
    assert mask_text("paid with 4111-1111-1111-1111, cvv 123") == "paid with ****-****-****-1111, cvv ***"
    assert mask_obj({"a": ["5555 5555 5555 4444"], "n": 3}) == {"a": ["****-****-****-4444"], "n": 3}
