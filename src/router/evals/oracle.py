"""Understanding taken from dataset labels, so decision/action/reply steps can be evaluated without an LLM."""

from __future__ import annotations

from router.dataset.loader import LoadedRecord
from router.decide.signals import ToneSignals
from router.pipeline.understanding import IntentDetail, Understanding, Understander


def label_oracle(records: list[LoadedRecord]) -> Understander:
    by_mid: dict[str, Understanding] = {}
    for lr in records:
        l = lr.record.label
        details = tuple(IntentDetail(i.intent, 1.0, "(label)", True, (i.order_id,) if i.order_id else tuple(i.entities.order_ids),
                                     tuple(i.entities.items), tuple(i.entities.amounts), tuple(i.entities.dates),
                                     i.entities.remedy or "none") for i in l.intents)
        by_mid.setdefault(lr.email.message_id, Understanding(
            intents=tuple(dict.fromkeys(i.intent for i in l.intents)),
            item_hints=tuple(h for i in l.intents for h in i.entities.items),
            language=l.language, code_mixed=l.code_mixed,
            tone=ToneSignals(anger=1.0 if "anger" in l.escalation else 0.0,
                             chargeback_threat=1.0 if "chargeback_threat" in l.escalation else 0.0,
                             public_complaint_threat=1.0 if "public_complaint_threat" in l.escalation else 0.0),
            injection=l.injection, details=details,
            order_ids=tuple(o for i in l.intents for o in i.entities.order_ids)))
    return lambda email: by_mid[email.message_id]
