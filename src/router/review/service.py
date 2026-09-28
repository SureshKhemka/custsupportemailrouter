"""Human review (FR-34..FR-36): queues, case view, approve / edit-and-approve / reject / reassign.

- Approving first runs the case's held or proposed actions under their original idempotency keys
  (so a retry or double click cannot duplicate them). If any fails, nothing is sent.
- The text (draft or edited) then passes the outbound gate with facts fetched fresh from the backends
  (FR-31). There is no override: a blocked reply must be edited.
- Every agent action is recorded append-only, with the edit size and whether the decision changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from router.clients import Backends, ServiceError
from router.config.models import Settings
from router.decide.identity import extract_order_ids
from router.decide.sla import sla_status
from router.gate import ActionRecord, CaseFacts, GateResult, run_gate
from router.pipeline.actions import _call
from router.store.db import Store


class ReviewError(Exception):
    pass


@dataclass
class CaseView:
    case: dict[str, Any]
    sla: str | None
    emails: list[dict[str, Any]]
    understanding: dict[str, Any] | None
    identity: dict[str, Any] | None
    decision: dict[str, Any] | None
    actions: list[dict[str, Any]]
    draft: dict[str, Any] | None
    facts: CaseFacts
    agent_actions: list[dict[str, Any]]


@dataclass
class ApproveResult:
    sent: bool
    message_id: str | None = None
    gate: GateResult | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    edit_class: str | None = None
    edit_size: float | None = None
    problem: str | None = None


class ReviewService:
    def __init__(self, store: Store, backends: Backends, cfg: Settings):
        self.store, self.backends, self.cfg = store, backends, cfg
        self.tz = ZoneInfo(cfg.app.timezone)

    # ------------------------------------------------------------------ queues (FR-35, FR-37)

    def sla(self, case: dict[str, Any], now: datetime) -> str | None:
        if not case.get("sla_due"):
            return None
        due = datetime.fromisoformat(case["sla_due"])
        created = datetime.fromisoformat(case["created_at"])
        return sla_status(created, due, now, self.cfg)

    def queues(self, now: datetime) -> list[dict[str, Any]]:
        out = []
        for q in self.cfg.routing.queues:
            cases = self.store.open_cases(q)
            states = [self.sla(c, now) for c in cases]
            out.append({"queue": q, "open": len(cases), "approaching": states.count("approaching"),
                        "breached": states.count("breached"), "priority": sum(bool(c["priority"]) for c in cases)})
        return out

    def list_cases(self, queue: str | None, now: datetime) -> list[dict[str, Any]]:
        return [{**c, "sla": self.sla(c, now)} for c in self.store.open_cases(queue)]

    # ------------------------------------------------------------------ case view (FR-34)

    def view(self, case_id: str, now: datetime) -> CaseView:
        case = self._case(case_id)
        events = self.store.events(case_id)
        latest = lambda kind: next((e["data"] for e in reversed(events) if e["kind"] == kind), None)  # noqa: E731
        emails = [e["data"] for e in events if e["kind"] == "email_received"]
        draft = self._pending_draft(case_id)
        facts = self.facts(case, draft["text"] if draft else "", now)
        return CaseView(case, self.sla(case, now), emails, latest("understanding"), latest("identity"),
                        latest("decision"), self.store.case_actions(case_id), draft, facts,
                        self.store.agent_actions(case_id))

    def facts(self, case: dict[str, Any], text: str, now: datetime) -> CaseFacts:
        """Case facts fetched fresh from the backends, for the view and the gate."""
        events = self.store.events(case["case_id"])
        ident = next((e["data"] for e in reversed(events) if e["kind"] == "identity"), {}) or {}
        decision = next((e["data"] for e in reversed(events) if e["kind"] == "decision"), {}) or {}
        customer = self.backends.customer.by_email(case["sender"]) if ident.get("ownership") == "owner" else None
        orders, refunds, returns, charges = [], {}, {}, {}
        for oid in ident.get("verified_order_ids", []):
            o = self.backends.order.get(oid)
            if o is None:
                continue
            orders.append(o)
            refunds[oid], returns[oid] = self.backends.refund.by_order(oid), self.backends.returns.by_order(oid)
            charges[oid] = self.backends.payments.by_order(oid)
        owners = {}
        for oid in extract_order_ids(text):
            o = next((x for x in orders if x.order_id == oid), None) or self.backends.order.get(oid)
            if o is not None:
                owners[oid] = o.customer_id
        amounts = set()
        for d in decision.get("intents", []):
            dec = d.get("decision") or {}
            for k in ("refund_amount", "amount"):
                if dec.get(k):
                    amounts.add(float(dec[k]))
        actions = [ActionRecord(a["type"], a["order_id"], a["status"] if a["status"] in {"succeeded", "failed"} else "not_run",
                                (a["request"] or {}).get("amount")) for a in self.store.case_actions(case["case_id"])]
        return CaseFacts(language=case.get("language") or "en", customer=customer,
                         sender_display_name=next((e["data"].get("sender_name") for e in events
                                                   if e["kind"] == "email_received"), None),
                         orders=orders, policy=self.cfg.policy, tz=self.tz, now=now, refunds=refunds, returns=returns,
                         charges=charges, decision_amounts=amounts, actions=actions, order_owner=owners)

    # ------------------------------------------------------------------ actions (FR-34, FR-36)

    def approve(self, case_id: str, agent: str, now: datetime, *, text: str | None = None,
                run_actions: bool = True) -> ApproveResult:
        case = self._open(case_id)
        draft = self._pending_draft(case_id)
        final = text if text is not None else (draft["text"] if draft else None)
        if not final or not final.strip():
            raise ReviewError("this case has no draft: write the reply (edit) before approving")

        pending = [a for a in self.store.case_actions(case_id) if a["status"] in {"proposed", "failed", "shadow"}]
        edit_size, edit_class = self._edit(draft["text"] if draft else None, final)

        # 0. pre-check: gate the reply as if the pending actions succeeded. If it would be blocked,
        #    do nothing at all: an action must never run for a reply that cannot be sent.
        pre = self.facts(case, final, now)
        if run_actions:
            pre.actions = [*pre.actions, *(ActionRecord(a["type"], a["order_id"], "succeeded",
                                                        (a["request"] or {}).get("amount")) for a in pending)]
        pre_gate = run_gate(final, pre)
        if not pre_gate.passed:
            self.store.add_agent_action(case_id=case_id, agent=agent, action="approve_blocked_by_gate", at=now,
                                        draft_seq=draft["seq"] if draft else None, edit_size=edit_size,
                                        edit_class=edit_class, detail={"gate": pre_gate.failures, "actions": []})
            return ApproveResult(False, gate=pre_gate, edit_class=edit_class, edit_size=edit_size,
                                 problem="blocked by the outbound gate; edit the reply (nothing was done)")

        # 1. held / proposed / previously failed actions, under their original idempotency keys
        ran = []
        if run_actions:
            for a in pending:
                try:
                    resp = _call(a["request"], case_id, a["key"], self.backends)
                    self.store.save_action(key=a["key"], case_id=case_id, type=a["type"], order_id=a["order_id"],
                                           status="succeeded", attempts=1, request=a["request"], response=resp,
                                           error=None, at=now)
                    ran.append({"key": a["key"], "type": a["type"], "status": "succeeded"})
                except ServiceError as exc:
                    self.store.save_action(key=a["key"], case_id=case_id, type=a["type"], order_id=a["order_id"],
                                           status="failed", attempts=1, request=a["request"], response=None,
                                           error=str(exc), at=now)
                    ran.append({"key": a["key"], "type": a["type"], "status": "failed", "error": str(exc)})
            if any(r["status"] == "failed" for r in ran):  # FR-25: never send a reply after a failed action
                self.store.add_agent_action(case_id=case_id, agent=agent, action="approve_blocked_action_failed",
                                            at=now, draft_seq=draft["seq"] if draft else None, detail={"actions": ran})
                return ApproveResult(False, actions=ran, problem="an action failed; nothing was sent")

        # 2. gate again with the real results and fresh facts (FR-31); no override
        facts = self.facts(case, final, now)
        gate = run_gate(final, facts)
        if not gate.passed:
            self.store.add_agent_action(case_id=case_id, agent=agent, action="approve_blocked_by_gate", at=now,
                                        draft_seq=draft["seq"] if draft else None, edit_size=edit_size,
                                        edit_class=edit_class, detail={"gate": gate.failures, "actions": ran})
            return ApproveResult(False, gate=gate, actions=ran, edit_class=edit_class, edit_size=edit_size,
                                 problem="blocked by the outbound gate; edit the reply")

        # 3. send, one reply per inbound email (idempotent)
        last = self._last_inbound(case_id)
        subject = next((e["subject"] for e in reversed(self.view_emails(case_id))), "") or ""
        try:
            msg = self.backends.outbox.send(case_id, case["sender"], f"Re: {subject}".strip(), final, facts.language,
                                            last, idempotency_key=f"{case_id}:reply:{last}")
        except ServiceError as exc:
            if exc.kind != "conflict":
                return ApproveResult(False, gate=gate, actions=ran, problem=f"sending failed: {exc}")
            msg = self.backends.outbox.send(case_id, case["sender"], f"Re: {subject}".strip(), final, facts.language,
                                            last, idempotency_key=f"{case_id}:reply:{last}:human")
        if draft:
            self.store.set_draft_status(draft["seq"], "approved" if edit_class == "unchanged" else "edited_and_approved")
        decision_changed = bool(pending) and not run_actions
        self.store.add_agent_action(case_id=case_id, agent=agent, action="approve", at=now,
                                    draft_seq=draft["seq"] if draft else None, edit_size=edit_size, edit_class=edit_class,
                                    decision_changed=decision_changed,
                                    detail={"message_id": msg.get("message_id"), "actions": ran,
                                            "skipped_actions": [a["key"] for a in pending] if not run_actions else []})
        self.store.update_case(case_id, now, status="closed", stage="closed", resolution="human_replied")
        return ApproveResult(True, msg.get("message_id"), gate, ran, edit_class, edit_size)

    def reject(self, case_id: str, agent: str, reason: str, now: datetime, *, close: bool = False,
               decision_changed: bool = False) -> None:
        if not reason.strip():
            raise ReviewError("a reason is required to reject")
        self._open(case_id)
        draft = self._pending_draft(case_id)
        if draft:
            self.store.set_draft_status(draft["seq"], "rejected")
        self.store.add_agent_action(case_id=case_id, agent=agent, action="reject", at=now,
                                    draft_seq=draft["seq"] if draft else None, reason=reason,
                                    decision_changed=decision_changed, detail={"closed": close})
        if close:
            self.store.update_case(case_id, now, status="closed", stage="closed", resolution="closed_no_reply")

    def reassign(self, case_id: str, agent: str, queue: str, now: datetime, reason: str = "") -> None:
        if queue not in self.cfg.routing.queues:
            raise ReviewError(f"unknown queue {queue!r}; queues: {self.cfg.routing.queues}")
        case = self._open(case_id)
        self.store.add_agent_action(case_id=case_id, agent=agent, action="reassign", at=now, reason=reason or None,
                                    detail={"from": case["queue"], "to": queue})
        self.store.update_case(case_id, now, queue=queue)

    # ------------------------------------------------------------------ helpers

    def view_emails(self, case_id: str) -> list[dict[str, Any]]:
        return [e["data"] for e in self.store.events(case_id) if e["kind"] == "email_received"]

    def _case(self, case_id: str) -> dict[str, Any]:
        case = self.store.get_case(case_id)
        if case is None:
            raise ReviewError(f"no case {case_id}")
        return case

    def _open(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        if case["status"] != "open" or case["stage"] != "awaiting_human":
            raise ReviewError(f"case {case_id} is not waiting for a human (status {case['status']}, stage {case['stage']})")
        return case

    def _pending_draft(self, case_id: str) -> dict[str, Any] | None:
        return next((d for d in reversed(self.store.drafts(case_id)) if d["status"] == "pending_review"), None)

    def _last_inbound(self, case_id: str) -> str | None:
        mails = [e for e in self.store.case_emails(case_id) if e.outcome in {"new_case", "follow_up"}]
        return mails[-1].message_id if mails else None

    def _edit(self, draft: str | None, final: str) -> tuple[float | None, str]:
        if draft is None:
            return None, "authored"
        if draft.strip() == final.strip():
            return 0.0, "unchanged"
        size = round(1 - SequenceMatcher(None, draft, final).ratio(), 3)
        return size, "light" if size <= self.cfg.review.light_edit_max else "heavy"
