"""Reply personalisation and agent summaries without a network (FR-27..FR-30, LL-1, LL-3)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.core.clock import FixedClock
from router.dataset.loader import load_records
from router.evals.oracle import label_oracle
from router.llm import LLMClient
from router.llm.providers import Completion
from router.pipeline.personalise import Personaliser, Summarizer, protected_facts, should_personalise
from router.pipeline.runner import Router
from router.store.db import Store

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-09-27T10:00:00+05:30")
DRAFT = ("Hi Kunal,\n\nI'm sorry the cookware from order ORD-100025 arrived in that condition. Since it's currently out "
         "of stock, we'd like to refund ₹5,499 to your original payment method.\n\nWarm regards,\nCustomer Care\n")


class Fake:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def complete(self, step, system, user, schema, name):
        self.calls += 1
        return Completion(self.outputs.pop(0), 800, 120, "stop")


def llm(loaded, outputs):
    return LLMClient(loaded, providers={"lmstudio": Fake(outputs)})


@pytest.fixture(scope="module")
def records(pinned_config):
    return {r.record.id: r for r in load_records(pinned_config.settings.paths.dataset)}


def test_protected_facts() -> None:
    assert protected_facts(DRAFT) == ["ORD-100025", "₹5,499", "Kunal"]


def test_should_personalise_follows_config(pinned_config, tmp_path) -> None:
    cfg = pinned_config.settings
    assert should_personalise("draft", ("damaged_item",), cfg) and not should_personalise("auto", ("order_status",), cfg)
    assert not should_personalise("identity", ("order_status",), cfg)
    o = tmp_path / "o.yaml"
    o.write_text(yaml.safe_dump({"replies": {"personalise": {"per_intent": {"order_status": True, "damaged_item": False}}}}))
    cfg2 = load_config([o], root=ROOT, use_local=False, env={}).settings
    assert should_personalise("auto", ("order_status",), cfg2) and not should_personalise("draft", ("damaged_item",), cfg2)


def test_rewrite_keeps_facts(pinned_config, records) -> None:
    good = DRAFT.replace("I'm sorry", "I'm really sorry to hear that")
    p = Personaliser(llm(pinned_config, [json.dumps({"reply": good})]), pinned_config.settings)(
        DRAFT, records["D062"].email, "en")
    assert p.used_llm and "really sorry" in p.text


def test_rewrite_that_drops_a_fact_falls_back_to_template(pinned_config, records) -> None:
    dropped = json.dumps({"reply": DRAFT.replace("₹5,499", "the full amount")})
    p = Personaliser(llm(pinned_config, [dropped] * 3), pinned_config.settings)(DRAFT, records["D062"].email, "en")
    assert not p.used_llm and p.text == DRAFT and "template used" in p.note


def test_rewrite_blocked_by_gate_is_not_used(pinned_config, mocks, records) -> None:
    """A rewrite that keeps the facts but adds a promise is caught by the gate; the template goes out."""
    promise = DRAFT.replace("Warm regards", "We guarantee the money by tomorrow.\n\nWarm regards")
    router = Router(pinned_config, Store(":memory:"), FixedClock(NOW), understander=label_oracle(list(records.values())),
                    http_clients=mocks)
    router.personaliser = Personaliser(llm(pinned_config, [json.dumps({"reply": promise})]), pinned_config.settings)
    ctx = router.process_email(records["D062"].email)
    ev = router.store.events(ctx.case_id, kind="personalisation")[-1]["data"]
    assert ev["used"] is False and "consistent_with_policy" in ev["gate"]
    assert "guarantee" not in router.store.drafts(ctx.case_id)[-1]["text"]


def test_summary_rejects_invented_order_ids(pinned_config, mocks, records) -> None:
    router = Router(pinned_config, Store(":memory:"), FixedClock(NOW), understander=label_oracle(list(records.values())),
                    http_clients=mocks)
    ok = {"summary": "Kunal Saxena (standard) reports cookware from ORD-100025 is peeling; policy gives a refund of 5499.",
          "customer_asks": ["refund"], "key_facts": ["ORD-100025 delivered, total 5499"], "risk_flags": [],
          "suggested_next_step": "Approve the refund draft."}
    bad = {**ok, "summary": ok["summary"] + " Also see ORD-100999."}
    router.summarizer = Summarizer(llm(pinned_config, [json.dumps(bad), json.dumps(ok)]), pinned_config.settings)
    ctx = router.process_email(records["D062"].email)
    assert router.store.get_case(ctx.case_id)["summary"]["summary"] == ok["summary"]
