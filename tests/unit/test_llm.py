"""LLM layer without a network: strict schemas, prompt files, retries on invalid output, failure -> human,
record/replay determinism (EV-1, LL-3), and the understanding mapping (FR-13, FR-15, FR-17)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.decide.routing import CaseInput, IntentInput, decide_case
from router.llm import LLMClient, LLMFailure
from router.llm.prompts import load_prompt
from router.llm.providers import Completion, ProviderError, clean_json_text
from router.llm.schema import strict_schema
from router.pipeline.understanding import LLMUnderstander
from router.schemas.email import InboundEmail
from router.schemas.understanding import UnderstandingOut, evidence_found

ROOT = Path(__file__).resolve().parents[2]
EMAIL = InboundEmail.model_validate({
    "message_id": "<t1@x>", "from": {"email": "kavya.menon@example.com", "name": "Kavya"}, "to": "s@x",
    "subject": "Where is ORD-100004?", "received_at": "2026-09-27T09:00:00+05:30",
    "body": "Hi, where is my air fryer ORD-100004? It is late. I will see you in consumer court."})


def good(**over) -> str:
    out = {"language": "en", "code_mixed": False, "tone": {"anger": 0.2, "chargeback_threat": 0.0,
                                                          "public_complaint_threat": 0.0},
           "injection_attempt": False, "injection_evidence": "",
           "intents": [{"intent": "order_status", "confidence": 0.95, "evidence": "where is my air fryer ORD-100004",
                        "order_ids": ["ORD-100004"], "items": ["air fryer"], "amounts": [], "dates": [],
                        "requested_remedy": "none"},
                       {"intent": "legal_threat", "confidence": 0.9, "evidence": "I will see you in consumer court",
                        "order_ids": [], "items": [], "amounts": [], "dates": [], "requested_remedy": "none"}]}
    out.update(over)
    return json.dumps(out)


class FakeProvider:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def complete(self, step, system, user, schema, name):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return Completion(out, 1000, 150, "stop")


@pytest.fixture
def loaded(tmp_path):
    o = tmp_path / "o.yaml"
    o.write_text(yaml.safe_dump({"paths": {"recordings": str(tmp_path / "rec")}}))
    return load_config([o], root=ROOT, use_local=False, env={})


def client(loaded, outputs, mode="off", calls=None):
    return LLMClient(loaded, providers={"lmstudio": FakeProvider(outputs)}, recording_mode=mode, recording_name="t",
                     on_call=(calls.append if calls is not None else None))


def test_strict_schema_is_provider_safe() -> None:
    s = strict_schema(UnderstandingOut, {"intent": ["a", "b"]})
    intent = s["properties"]["intents"]["items"]
    assert intent["properties"]["intent"]["enum"] == ["a", "b"]
    assert intent["additionalProperties"] is False and set(intent["required"]) == set(intent["properties"])
    assert "$defs" not in s and "minimum" not in json.dumps(s) and "$ref" not in json.dumps(s)


def test_prompt_file_renders_strictly(loaded) -> None:
    p = load_prompt(loaded.settings.paths.prompts, "understand", "v1")
    with pytest.raises(Exception):
        p.render(taxonomy=[])  # missing variables fail loudly
    system, user = p.render(taxonomy=[("order_status", "d")], sender_name="K", subject="s", attachments="none", body="b")
    assert "<email>" in user and "order_status" in system


def test_valid_output_first_try(loaded) -> None:
    calls = []
    u = LLMUnderstander(client(loaded, [good()], calls=calls), loaded.settings)(EMAIL)
    assert u.intents == ("order_status", "legal_threat") and not u.uncertain and u.order_ids == ("ORD-100004",)
    assert calls[0].ok and calls[0].attempts == 1
    assert calls[0].prompt_version == loaded.settings.llm.steps["understand"].prompt_version


def test_invalid_output_is_retried_then_accepted(loaded) -> None:
    calls = []
    bad = good(language="English")
    u = LLMUnderstander(client(loaded, ["not json", bad, good()], calls=calls), loaded.settings)(EMAIL)
    assert not u.failed and calls[-1].attempts == 3 and len(calls[-1].attempt_errors) == 2


def test_persistent_invalid_output_fails_to_human(loaded) -> None:
    u = LLMUnderstander(client(loaded, ["{}", "{}", "{}"]), loaded.settings)(EMAIL)
    assert u.failed
    h = decide_case(CaseInput(intents=(), understanding_failed=True), loaded.settings)
    assert (h.mode, h.disposition, h.flags) == ("ROUTE", "routed", ("understanding_failed",))


def test_non_retryable_provider_error_stops(loaded) -> None:
    fake = FakeProvider([ProviderError("HTTP 400", retryable=False), good()])
    llm = LLMClient(loaded, providers={"lmstudio": fake})
    assert LLMUnderstander(llm, loaded.settings)(EMAIL).failed and fake.calls == 1


def test_unknown_intent_rejected(loaded) -> None:
    out = json.loads(good())
    out["intents"][0]["intent"] = "make_coffee"
    assert LLMUnderstander(client(loaded, [json.dumps(out)] * 3), loaded.settings)(EMAIL).failed


def test_record_then_replay_is_identical_and_makes_no_calls(loaded) -> None:
    first = LLMUnderstander(client(loaded, [good()], mode="record"), loaded.settings)(EMAIL)
    replay_llm = client(loaded, [], mode="replay")
    again = LLMUnderstander(replay_llm, loaded.settings)(EMAIL)
    assert again == first and replay_llm.provider("lmstudio").calls == 0
    other = EMAIL.model_copy(update={"body": "different"})
    assert LLMUnderstander(client(loaded, [], mode="replay"), loaded.settings)(other).failed  # replay miss


def test_low_confidence_or_missing_evidence_is_uncertain(loaded) -> None:
    out = json.loads(good())
    out["intents"][0]["confidence"] = 0.4  # threshold 0.80
    out["intents"][1]["evidence"] = "text that is not in the email at all"
    u = LLMUnderstander(client(loaded, [json.dumps(out)]), loaded.settings)(EMAIL)
    assert set(u.uncertain) == {"order_status", "legal_threat"}
    h = decide_case(CaseInput(intents=(IntentInput("order_status"),), uncertain=True), loaded.settings)
    assert h.mode == "ROUTE" and "uncertain_intent" in h.flags


def test_uncertain_spam_is_never_dropped(loaded) -> None:
    h = decide_case(CaseInput(intents=(IntentInput("spam_or_auto"),), uncertain=True), loaded.settings)
    assert h.disposition == "routed"


def test_hallucinated_order_ids_are_ignored(loaded) -> None:
    out = json.loads(good())
    out["intents"][0]["order_ids"] = ["ORD-100004", "ORD-999999", "#100005"]
    u = LLMUnderstander(client(loaded, [json.dumps(out)]), loaded.settings)(EMAIL)
    assert u.order_ids == ("ORD-100004",)


def test_evidence_matching() -> None:
    body = "Hi, where is my air fryer ORD-100004? It's late!!"
    assert evidence_found("Where is my air-fryer ORD-100004", body)
    assert not evidence_found("I want a refund", body)


def test_clean_json_text() -> None:
    assert clean_json_text('<think>hmm</think>\n```json\n{"a": 1}\n```') == '{"a": 1}'
