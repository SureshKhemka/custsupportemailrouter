"""Stable identifiers (FR-24, FR-40, NF-5).

Case ids and action idempotency keys are derived only from the email thread and the action,
never from time or randomness, so reprocessing the inbox from scratch (even with an empty
router database) produces the same keys and the backends refuse to repeat an action.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def normalize_message_id(message_id: str) -> str:
    return message_id.strip().strip("<>").strip().lower()


def case_id_for(thread_root_message_id: str) -> str:
    """One case per thread: the id is derived from the thread's first message id."""
    digest = hashlib.sha256(normalize_message_id(thread_root_message_id).encode()).hexdigest()
    return f"C-{digest[:12]}"


def action_key(case_id: str, action_type: str, order_id: str,
               lines: Iterable[tuple[str, int]] = (), amount: float | None = None) -> str:
    """Idempotency key for one action. A retry or replay of the same action yields the same key;
    a genuinely different action in the same case (other lines, other amount) yields a new key."""
    payload = json.dumps({"lines": sorted((l, int(q)) for l, q in lines),
                          "amount": None if amount is None else round(float(amount), 2)}, sort_keys=True)
    return f"{case_id}:{action_type}:{order_id}:{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


def step_id(case_id: str, seq: int, name: str) -> str:
    """Step ids share the case id so a tracing tool can be attached later (FR-40)."""
    return f"{case_id}/{seq:03d}-{name}"
