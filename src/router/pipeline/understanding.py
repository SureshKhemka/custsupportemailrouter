"""What the understanding step hands to the rest of the pipeline.

M6 fills this from a schema-validated LLM output. Tests and component evals can plug in an
oracle built from dataset labels, so decision steps can be evaluated without any LLM (EV-4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from router.decide.routing import ORDER_BOUND
from router.decide.signals import ToneSignals
from router.schemas.email import InboundEmail


@dataclass(frozen=True)
class Understanding:
    intents: tuple[str, ...]
    item_hints: tuple[str, ...] = ()
    language: str = "en"
    tone: ToneSignals = field(default_factory=ToneSignals)
    injection: bool = False

    @property
    def order_bound(self) -> bool:
        return any(i in ORDER_BOUND for i in self.intents)


Understander = Callable[[InboundEmail], Understanding]
