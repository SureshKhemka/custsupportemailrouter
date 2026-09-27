"""What the understanding step hands to the rest of the pipeline.

Produced by the LLM (`LLMUnderstander`), by recorded outputs in replay, or by an oracle built from
dataset labels, so decision steps can be evaluated without any LLM (EV-4).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from router.config.models import Settings
from router.decide.identity import extract_order_ids
from router.decide.routing import ORDER_BOUND
from router.decide.signals import ToneSignals
from router.llm import LLMClient, LLMFailure
from router.schemas.email import InboundEmail
from router.schemas.understanding import UnderstandingOut, evidence_found, validate_understanding


@dataclass(frozen=True)
class IntentDetail:
    intent: str
    confidence: float
    evidence: str
    evidence_found: bool
    order_ids: tuple[str, ...] = ()
    items: tuple[str, ...] = ()
    amounts: tuple[float, ...] = ()
    dates: tuple[str, ...] = ()
    remedy: str = "none"


@dataclass(frozen=True)
class Understanding:
    intents: tuple[str, ...]  # best guess, including uncertain ones
    item_hints: tuple[str, ...] = ()
    language: str = "en"
    tone: ToneSignals = field(default_factory=ToneSignals)
    injection: bool = False
    code_mixed: bool = False
    uncertain: tuple[str, ...] = ()  # below the per-intent threshold, or evidence not in the email (FR-15)
    details: tuple[IntentDetail, ...] = ()
    order_ids: tuple[str, ...] = ()  # order ids the model found that also appear in the email text
    injection_evidence: str = ""
    failed: bool = False  # no valid output after retries (LL-3) -> human
    failure: str | None = None

    @property
    def order_bound(self) -> bool:
        return self.failed or any(i in ORDER_BOUND for i in self.intents)


Understander = Callable[[InboundEmail], Understanding]


def describe_attachments(email: InboundEmail) -> str:
    if not email.attachments:
        return "none"
    images = [a.filename for a in email.attachments if a.is_image]
    other = [a.filename for a in email.attachments if not a.is_image]
    parts = [f"{len(images)} image(s): {', '.join(images)}"] if images else []
    if other:
        parts.append(f"{len(other)} other file(s): {', '.join(other)}")
    return "; ".join(parts)


class LLMUnderstander:
    def __init__(self, llm: LLMClient, cfg: Settings):
        self.llm, self.cfg = llm, cfg
        self.taxonomy = list(cfg.taxonomy.intents)

    def __call__(self, email: InboundEmail) -> Understanding:
        variables = {
            "taxonomy": [(n, c.description) for n, c in self.cfg.taxonomy.intents.items()],
            "sender_name": email.from_.name or "(no name)", "subject": email.subject or "(no subject)",
            "attachments": describe_attachments(email), "body": email.body,
        }
        try:
            res = self.llm.run("understand", UnderstandingOut, variables, enums={"intent": self.taxonomy},
                               validate=lambda o: validate_understanding(o, set(self.taxonomy)))
        except LLMFailure as exc:
            return Understanding(intents=(), failed=True, failure=str(exc))
        return to_understanding(res.parsed, email, self.cfg)


_DIGITS = re.compile(r"\d{6}")


def to_understanding(out: UnderstandingOut, email: InboundEmail, cfg: Settings) -> Understanding:
    text = f"{email.subject}\n{email.body}"
    details, uncertain, intents, hints, oids = [], [], [], [], []
    for i in out.intents:
        found = evidence_found(i.evidence, text)
        ids = tuple(o for o in extract_order_ids(" ".join(f"ORD-{d}" for x in i.order_ids for d in _DIGITS.findall(x)))
                    if o[4:] in text)  # never trust an order id that is not in the email
        details.append(IntentDetail(i.intent, i.confidence, i.evidence, found, ids, tuple(i.items),
                                    tuple(i.amounts), tuple(i.dates), i.requested_remedy))
        if i.intent not in intents:
            intents.append(i.intent)
        threshold = cfg.taxonomy.intents[i.intent].threshold
        if (i.confidence < threshold or not found) and i.intent not in uncertain:
            uncertain.append(i.intent)
        hints += [h for h in i.items if h not in hints]
        oids += [o for o in ids if o not in oids]
    # An intent is uncertain only if no mention of it is confident.
    confident = {d.intent for d in details if d.evidence_found and d.confidence >= cfg.taxonomy.intents[d.intent].threshold}
    uncertain = [u for u in uncertain if u not in confident]
    return Understanding(
        intents=tuple(intents), item_hints=tuple(hints), language=out.language, code_mixed=out.code_mixed,
        tone=ToneSignals(out.tone.anger, out.tone.chargeback_threat, out.tone.public_complaint_threat),
        injection=out.injection_attempt, injection_evidence=out.injection_evidence, uncertain=tuple(uncertain),
        details=tuple(details), order_ids=tuple(oids))
