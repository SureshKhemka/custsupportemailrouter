"""Intake (FR-2..FR-4, FR-6): duplicates, threads, near-duplicate candidates, automated mail.

Intake never calls an LLM. It decides which case an email belongs to:
- same message id seen before           -> duplicate, ignored (FR-2)
- replies to a known message / thread   -> follow-up on that case (FR-4)
- same sender, same orders, within the
  near-duplicate window, not a reply    -> candidate near-duplicate of that case; the merge is
                                           confirmed once intents are known (FR-3)
- otherwise                             -> new case, id derived from the thread root (FR-24)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from router.config.models import Settings
from router.core.ids import case_id_for, normalize_message_id
from router.decide.identity import extract_order_ids
from router.schemas.email import InboundEmail
from router.store.db import Store

Outcome = Literal["new_case", "follow_up", "duplicate"]


@dataclass(frozen=True)
class IntakeResult:
    outcome: Outcome
    case_id: str
    message_id: str
    order_ids: list[str]  # mentioned in this email
    automated: str | None = None  # reason, if headers/sender mark it as automated mail
    near_duplicate_of: str | None = None  # candidate case to merge into (confirmed after understanding)


def automated_reason(email: InboundEmail, cfg: Settings) -> str | None:
    h = {k.lower(): v.strip().lower() for k, v in email.headers.items()}
    local = email.sender.split("@", 1)[0]
    if any(local.startswith(p) for p in cfg.intake.auto_sender_prefixes):
        return f"automated sender '{local}'"
    if h.get("auto-submitted", "no") != "no":
        return f"Auto-Submitted: {h['auto-submitted']}"
    if "x-autoreply" in h or "x-autorespond" in h:
        return "X-Autoreply header"
    if h.get("precedence") in {"bulk", "junk", "list", "auto_reply"}:
        return f"Precedence: {h['precedence']}"
    if "list-unsubscribe" in h or "list-id" in h:
        return "mailing-list headers"
    if h.get("content-type", "").startswith("multipart/report"):
        return "delivery status report"
    return None


def intake(email: InboundEmail, store: Store, cfg: Settings) -> IntakeResult:
    mid = normalize_message_id(email.message_id)
    ids = extract_order_ids(f"{email.subject}\n{email.body}")
    auto = automated_reason(email, cfg)

    seen = store.first_email(mid)
    if seen:
        return IntakeResult("duplicate", seen.case_id or case_id_for(mid), mid, ids, auto)

    for ref in (email.in_reply_to, email.thread_id):
        if ref:
            parent = store.first_email(normalize_message_id(ref))
            if parent and parent.case_id:
                return IntakeResult("follow_up", parent.case_id, mid, ids, auto)

    root = normalize_message_id(email.thread_id or email.in_reply_to or email.message_id)
    candidate = None
    if not (email.in_reply_to or email.thread_id) and ids and not auto:
        window = timedelta(minutes=cfg.intake.near_duplicate_window_minutes)
        for prev in reversed(store.emails_from(email.sender, email.received_at - window)):
            if prev.outcome in {"new_case", "follow_up"} and prev.received_at <= email.received_at \
                    and set(prev.order_ids) == set(ids):
                candidate = prev.case_id
                break
    return IntakeResult("new_case", case_id_for(root), mid, ids, auto, candidate)


