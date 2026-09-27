"""Processing loop: inbox -> intake -> identity -> (later milestones: understand, decide, act, reply, gate).

Each email is processed on its own; a failure is recorded and never stops the others (NF-4).
Every step and backend call is written to the append-only audit log (FR-39).
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from router.clients import Backends, CallRecord
from router.config import LoadedConfig
from router.core.clock import Clock
from router.core.ids import step_id
from router.llm import LLMCall
from router.pipeline.identify import Identity, identify
from router.pipeline.intake import IntakeResult, intake
from router.pipeline.understanding import Understander, Understanding
from router.schemas.email import InboundEmail
from router.store.db import Store


@dataclass
class CaseContext:
    case_id: str
    email: InboundEmail
    now: datetime
    intake: IntakeResult
    source: str | None = None
    understanding: Understanding | None = None
    identity: Identity | None = None
    stage: str = "received"
    step_seq: int = 0

    def next_step(self, name: str) -> str:
        self.step_seq += 1
        return step_id(self.case_id, self.step_seq, name)


@dataclass
class RunSummary:
    run_id: str
    outcomes: Counter = field(default_factory=Counter)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (source, error)


class Router:
    def __init__(self, loaded: LoadedConfig, store: Store, clock: Clock, *,
                 understander: Understander | None = None,
                 http_clients: dict[str, httpx.Client] | None = None):
        """`understander`: the understanding step (LLM, replay or label oracle); None = not wired yet.
        `http_clients` swaps the network transport per service (tests use in-process mock apps)."""
        self.loaded, self.cfg, self.store, self.clock = loaded, loaded.settings, store, clock
        self.understander = understander
        self._current: CaseContext | None = None
        self.backends = Backends.from_config(self.cfg, on_call=self._record_call, http_clients=http_clients,
                                             sleep=(lambda _s: None) if http_clients else None)

    # ------------------------------------------------------------------ entry points

    def start_run(self, kind: str = "process") -> str:
        run_id = f"RUN-{uuid.uuid4().hex[:10]}"
        self.store.record_run(run_id, self.clock.now(), kind, self.loaded.effective())  # CF-6
        return run_id

    def process_inbox(self, inbox: Path) -> RunSummary:
        summary = RunSummary(self.start_run())
        emails: list[tuple[InboundEmail, str]] = []
        for path in sorted(inbox.glob("*.json")):
            try:
                emails.append((InboundEmail.model_validate(json.loads(path.read_text(encoding="utf-8"))), str(path)))
            except (ValueError, ValidationError) as exc:  # unreadable file: record and move on
                summary.failures.append((str(path), f"invalid email file: {exc}".splitlines()[0]))
                self.store.append_event(None, None, "intake_error", {"source": str(path), "error": str(exc)[:2000]},
                                        self.clock.now())
        for email, source in sorted(emails, key=lambda e: (e[0].received_at, e[1])):
            try:
                ctx = self.process_email(email, source)
                summary.outcomes[ctx.stage] += 1
            except Exception as exc:  # NF-4: one failure never stops the run
                summary.failures.append((source, f"{type(exc).__name__}: {exc}"))
                summary.outcomes["error"] += 1
        return summary

    def process_email(self, email: InboundEmail, source: str | None = None) -> CaseContext:
        now = email.received_at if self.cfg.clock.process_at_received_time else self.clock.now()
        ir = intake(email, self.store, self.cfg)
        # Step ids keep counting across every email of the case, so they stay unique (FR-40).
        ctx = CaseContext(ir.case_id, email, now, ir, source, step_seq=self.store.count_events(ir.case_id))
        self._current = ctx
        try:
            return self._process(ctx)
        except Exception as exc:
            self.store.append_event(ctx.case_id, ctx.next_step("error"), "processing_error",
                                    {"error": f"{type(exc).__name__}: {exc}", "stage": ctx.stage}, now)
            if self.store.get_case(ctx.case_id):
                self.store.update_case(ctx.case_id, now, stage="error")
            ctx.stage = "error"
            raise
        finally:
            self._current = None

    # ------------------------------------------------------------------ steps

    def _process(self, ctx: CaseContext) -> CaseContext:
        email, ir, now = ctx.email, ctx.intake, ctx.now
        if ir.outcome == "duplicate":
            self._record_email(ctx, "duplicate")
            self.store.append_event(ctx.case_id, ctx.next_step("intake"), "duplicate_ignored",
                                    {"message_id": ir.message_id, "source": ctx.source}, now)
            ctx.stage = "duplicate_ignored"
            return ctx

        if ir.outcome == "new_case" and not self.store.get_case(ctx.case_id):
            self.store.create_case(ctx.case_id, thread_root=ir.message_id, sender=email.sender, at=now)
        self._record_email(ctx, ir.outcome)
        self.store.append_event(ctx.case_id, ctx.next_step("intake"), "email_received", {
            "outcome": ir.outcome, "message_id": ir.message_id, "source": ctx.source, "sender": email.sender,
            "subject": email.subject, "body": email.body, "received_at": email.received_at.isoformat(),
            "in_reply_to": email.in_reply_to, "attachments": [a.model_dump() for a in email.attachments],
            "order_ids_in_text": ir.order_ids, "automated": ir.automated, "near_duplicate_of": ir.near_duplicate_of,
        }, now)

        if ir.automated:  # FR-6: automated mail is closed without a reply and without an LLM call
            self.store.update_case(ctx.case_id, now, status="closed", stage="closed", disposition="closed_spam",
                                   mode="CLOSE", intents=["spam_or_auto"], flags=["automated"])
            self.store.append_event(ctx.case_id, ctx.next_step("intake"), "closed_automated",
                                    {"reason": ir.automated}, now)
            ctx.stage = "closed_automated"
            return ctx

        if self.understander is not None:
            ctx.understanding = self.understander(email)
            u = ctx.understanding
            self.store.append_event(ctx.case_id, ctx.next_step("understand"), "understanding", {
                "intents": list(u.intents), "uncertain": list(u.uncertain), "item_hints": list(u.item_hints),
                "language": u.language, "code_mixed": u.code_mixed, "tone": vars(u.tone), "injection": u.injection,
                "injection_evidence": u.injection_evidence, "order_ids": list(u.order_ids),
                "details": [vars(d) for d in u.details], "failed": u.failed, "failure": u.failure}, now)
        u = ctx.understanding
        mentioned = list(dict.fromkeys([*ir.order_ids, *(u.order_ids if u else ())]))
        ident = identify(email, ctx.case_id, mentioned, self.backends, self.store, self.cfg, now,
                         order_bound=u.order_bound if u else True, item_hints=list(u.item_hints) if u else [])
        ctx.identity = ident
        self.store.append_event(ctx.case_id, ctx.next_step("identify"), "identity", _identity_event(ident), now)
        self.store.update_case(ctx.case_id, now, stage="identified",
                               customer_id=ident.customer.customer_id if ident.customer else None,
                               primary_order_id=ident.primary_order_id)
        ctx.stage = "identified"
        return ctx

    # ------------------------------------------------------------------ helpers

    def _record_email(self, ctx: CaseContext, outcome: str) -> None:
        e = ctx.email
        self.store.record_email(message_id=ctx.intake.message_id, case_id=ctx.case_id, sender=e.sender,
                                received_at=e.received_at, subject=e.subject, source=ctx.source,
                                order_ids=ctx.intake.order_ids, outcome=outcome, at=ctx.now)

    def record_llm_call(self, call: LLMCall) -> None:
        """Hook for LLMClient(on_call=...): every model call goes to the audit log (FR-39, LL-5)."""
        ctx = self._current
        self.store.append_event(ctx.case_id if ctx else None, ctx.next_step(f"llm-{call.step}") if ctx else None,
                                "llm_call", asdict(call), ctx.now if ctx else self.clock.now())

    def _record_call(self, call: CallRecord) -> None:
        ctx = self._current
        self.store.append_event(ctx.case_id if ctx else None, ctx.next_step(f"call-{call.service}") if ctx else None,
                                "backend_call", asdict(call), ctx.now if ctx else self.clock.now())


def _identity_event(i: Identity) -> dict:
    return {
        "customer_id": i.customer.customer_id if i.customer else None,
        "tier": i.customer.tier if i.customer else None,
        "ownership": i.ownership,
        "mentioned_order_ids": i.mentioned_order_ids,
        "unknown_order_ids": i.unknown_order_ids,
        "inherited_from_case": i.inherited_from_case,
        "resolution": {"status": i.resolution.status, "order_ids": list(i.resolution.order_ids),
                       "candidates": list(i.resolution.candidates)},
        "verified_order_ids": list(i.verified),
        "recent_order_ids": i.recent_order_ids,
        "contact_count": i.contact_count,
    }
