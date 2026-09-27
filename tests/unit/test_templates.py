"""Every reply template branch renders clean text (no Python/Jinja artifacts) and passes the placeholder check.

Regression for M8 finding: `o.items` rendered dict.items (a method) into customer replies.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from router.config import load_config
from router.gate import extract as X

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = re.compile(r"<built-in|object at 0x|<bound|\{\{|\{%|None|Undefined|\[\]")


@pytest.fixture(scope="module")
def env():
    return Environment(loader=FileSystemLoader(str(ROOT / "config" / "templates" / "en")), undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True)


@pytest.fixture(scope="module")
def policy():
    return load_config(root=ROOT, use_local=False, env={}).settings.policy


O = {"order_id": "ORD-100004", "products": "Air Fryer 4L", "carrier": "Ekart", "tracking": "EK8667526689IN",
     "promised": "29 September 2026", "promised_passed": False, "delivered": "22 September 2026",
     "cancelled": "20 September 2026", "last_update": "18 September 2026"}

CASES = [
    ("order_status.j2", {"state": s}) for s in ("not_shipped", "in_transit", "out_for_delivery", "delayed", "lost",
                                                "delivered", "cancelled", "returned")
] + [
    ("return_request.j2", {"action": a, "reason": r, "lines_text": "Slim Fit Jeans", "category": "hygiene",
                           "requested_remedy": "exchange", "pickup": "29 September 2026"})
    for a, r in [("succeeded", "eligible"), ("held", "eligible"), ("failed", "eligible"), (None, "outside_window"),
                 (None, "non_returnable"), (None, "not_delivered"), (None, "already_returned"),
                 (None, "nothing_left_to_return")]
] + [
    ("damaged_item.j2", {"remedy": m, "reason": r, "lines_text": "", "kind": "damaged", "amount": "₹5,499",
                         "photo_threshold": "₹2,000", "requested_remedy": "refund"})
    for m, r in [("replacement", "ok"), ("refund", "ok"), ("request_photo", "photo_required"), ("none", "reported_late"),
                 ("none", "not_delivered")]
] + [
    ("cancel_order.j2", {"action": a, "cancellable": c, "returnable": True})
    for a, c in [("succeeded", True), ("held", True), ("failed", True), (None, False)]
] + [
    ("refund_status.j2", {"state": s, "amount": "₹6,999", "return_open": True})
    for s in ("none", "initiated", "processed", "failed")
]


@pytest.mark.parametrize("name,ctx", CASES)
def test_template_branch_renders_clean(env, policy, name, ctx) -> None:
    text = env.get_template(name).render(o=O, policy=policy, **ctx).strip()
    assert text, f"{name} {ctx} rendered nothing"
    assert not ARTIFACT.search(text), text
    assert not X.placeholders(text), text


def test_wrappers_render_clean(env) -> None:
    for name, ctx in [("reply.j2", {"first_name": "Kavya", "parts": ["A.", "B."]}), ("identity.j2", {}),
                      ("general.j2", {}), ("no_order.j2", {}),
                      ("billing.j2", {"o": O, "duplicate": True, "failed": False, "paid": True, "amount": "₹1,599"}),
                      ("billing.j2", {"o": O, "duplicate": False, "failed": True, "paid": False, "amount": None}),
                      ("billing.j2", {"o": None, "duplicate": False, "failed": False, "paid": False, "amount": None}),
                      ("clarify.j2", {"candidates": [{"order_id": "ORD-1", "products": "Lamp", "placed": "1 May 2026"}]})]:
        text = env.get_template(name).render(**ctx)
        assert not ARTIFACT.search(text) and not X.placeholders(text), (name, text)
