"""Actions (FR-23..FR-26): idempotent backend calls, recorded before and after.

- Each action's idempotency key comes from the case and the action (D-029), so a retry, a replayed
  email or a reprocessed inbox can never create a second return, refund or replacement.
- An action the router already recorded as succeeded is never sent again.
- A failed or timed-out action is recorded as failed; nothing downstream may claim it happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from router.clients import Backends, ServiceError
from router.core.ids import action_key
from router.pipeline.decide_step import IntentDecision
from router.store.db import Store


@dataclass(frozen=True)
class ActionResult:
    key: str
    intent: str
    type: str
    order_id: str
    status: str  # succeeded | failed | proposed | shadow
    amount: float | None = None
    lines: tuple[tuple[str, int], ...] = field(default=())
    response: Any = None
    error: str | None = None


def run_actions(case_id: str, decisions: list[IntentDecision], run_for: tuple[str, ...], shadow: bool,
                backends: Backends, store: Store, now: datetime) -> list[ActionResult]:
    results = []
    for d in decisions:
        a = d.action
        if a is None:
            continue
        lines = tuple((l, q) for l, q in a.get("lines", []))
        amount = a.get("amount")
        key = action_key(case_id, a["type"], a["order_id"], lines, amount)
        if d.intent not in run_for:
            results.append(_save(store, case_id, d.intent, a, key, "proposed", 0, None, None, now))
            continue
        if shadow:
            results.append(_save(store, case_id, d.intent, a, key, "shadow", 0, None, None, now))
            continue
        prev = store.get_action(key)
        if prev and prev["status"] == "succeeded":  # already done for this case: never repeat (FR-24)
            results.append(ActionResult(key, d.intent, a["type"], a["order_id"], "succeeded", amount, lines,
                                        prev["response"]))
            continue
        try:
            response = _call(a, case_id, key, backends)
            results.append(_save(store, case_id, d.intent, a, key, "succeeded", 1, response, None, now))
        except ServiceError as exc:
            results.append(_save(store, case_id, d.intent, a, key, "failed", 1, None, str(exc), now))
    return results


def _call(a: dict[str, Any], case_id: str, key: str, b: Backends) -> Any:
    t = a["type"]
    if t == "create_return":
        return b.returns.create(a["order_id"], [tuple(x) for x in a["lines"]], a["reason"], case_id, key)
    if t == "cancel_order":
        return b.order.cancel(a["order_id"], a["reason"], case_id, key)
    if t == "create_replacement":
        return b.replacement.create(a["order_id"], [tuple(x) for x in a["lines"]], a["reason"], case_id, key)
    if t == "issue_refund":
        return b.refund.create(a["order_id"], a["amount"], a["reason"], case_id, key)
    raise ValueError(f"unknown action {t}")


def _save(store: Store, case_id: str, intent: str, a: dict[str, Any], key: str, status: str, attempts: int,
          response: Any, error: str | None, now: datetime) -> ActionResult:
    store.save_action(key=key, case_id=case_id, type=a["type"], order_id=a["order_id"], status=status,
                      attempts=attempts, request=a, response=response, error=error, at=now)
    return ActionResult(key, intent, a["type"], a["order_id"], status, a.get("amount"),
                        tuple((l, q) for l, q in a.get("lines", [])), response, error)
