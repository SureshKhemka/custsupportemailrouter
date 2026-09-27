"""Gate and send (FR-31..FR-33), drafts for humans (FR-20), failure handling (FR-25, FR-26).

- AUTO + live: the reply passes the outbound gate, then goes to the outbox with an idempotency key
  per inbound email, so a replay can never send it twice (NF-5). A gate failure routes the case to
  the gate_failures queue instead (FR-32).
- DRAFT / ROUTE: the draft (if drafting is allowed) is stored with its gate result for the agent.
- shadow: nothing is sent; the would-be reply and gate result are recorded (FR-22).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from router.clients import Backends, ServiceError
from router.config.models import Settings
from router.core.ids import normalize_message_id
from router.decide.identity import extract_order_ids
from router.gate import ActionRecord, CaseFacts, GateResult, run_gate
from router.pipeline.actions import ActionResult
from router.pipeline.decide_step import Decision
from router.pipeline.identify import Identity
from router.pipeline.reply import ComposedReply
from router.pipeline.understanding import Understanding
from router.schemas.email import InboundEmail


@dataclass(frozen=True)
class Outcome:
    mode: str
    disposition: str
    queue: str | None
    priority: bool
    flags: tuple[str, ...]
    sent: bool
    gate: GateResult | None


def gate_facts(u: Understanding, identity: Identity, decision: Decision, actions: list[ActionResult],
               email: InboundEmail, reply: str, backends: Backends, cfg: Settings, now: datetime) -> CaseFacts:
    verified = identity.verified if identity.is_verified else {}
    orders = [f.order for f in verified.values()]
    if decision.handling.disposition == "clarification_requested" and identity.customer is not None:
        # The question lists the sender's own recent orders; those are facts of this case too.
        orders += [o for oid in identity.resolution.candidates
                   if (o := backends.order.get(oid)) and o.customer_id == identity.customer.customer_id]
    owners = {}
    for oid in extract_order_ids(reply):  # who owns each order the reply mentions (other-customer data check)
        o = next((x for x in orders if x.order_id == oid), None) or backends.order.get(oid)
        if o is not None:
            owners[oid] = o.customer_id
    return CaseFacts(
        language=u.language, customer=identity.customer if identity.is_verified else None,
        sender_display_name=email.from_.name, orders=orders, policy=cfg.policy,
        tz=ZoneInfo(cfg.app.timezone), now=now,
        refunds={k: f.refunds for k, f in verified.items()}, returns={k: f.returns for k, f in verified.items()},
        charges={k: f.charges for k, f in verified.items()},
        decision_amounts={float(d.decision["refund_amount"]) for d in decision.intents
                          if d.decision.get("refund_amount")} | {float(d.decision["amount"]) for d in decision.intents
                                                                 if d.intent == "refund_status" and d.decision.get("amount")},
        actions=[ActionRecord(a.type, a.order_id, a.status if a.status in {"succeeded", "failed"} else "not_run", a.amount)
                 for a in actions],
        order_owner=owners,
    )


def deliver(case_id: str, email: InboundEmail, decision: Decision, reply: ComposedReply | None,
            facts: CaseFacts | None, action_failed: bool, backends: Backends, store, cfg: Settings,
            now: datetime) -> tuple[Outcome, dict]:
    h, dv = decision.handling, decision.delivery
    mode, disposition, queue, flags = dv.mode, h.disposition, h.queue, list(h.flags)
    if dv.mode == "DRAFT" and h.disposition == "auto_replied":
        disposition = "drafted"  # draft_only turned an AUTO case into a draft
        queue = queue or "general"
    if action_failed:  # FR-25 / FR-26: a human takes over; nothing may claim the failed action
        mode, disposition, queue = "ROUTE", "routed", queue if queue and queue != "general" else "general"
        flags.append("action_failed")

    gate = run_gate(reply.text, facts) if reply and facts else None
    event: dict = {"reply": reply.text if reply else None, "kind": reply.kind if reply else None,
                   "gate": gate.failures if gate else None}
    sent = False
    auto = mode == "AUTO" and reply is not None
    if dv.shadow:
        event["shadow"] = True
    elif auto and dv.send_reply and not action_failed:
        if gate and gate.passed:
            try:
                mid = normalize_message_id(email.message_id)
                msg = backends.outbox.send(case_id, email.sender, f"Re: {email.subject}".strip(), reply.text,
                                           facts.language, email.message_id, idempotency_key=f"{case_id}:reply:{mid}")
                event["message_id"] = msg.get("message_id")
                sent = True
            except ServiceError as exc:  # FR-25: the send itself failed -> human
                mode, disposition, queue = "ROUTE", "routed", "general"
                flags.append("send_failed")
                event["send_error"] = str(exc)
        else:  # FR-32: blocked by the gate -> human, with the reasons
            mode, disposition, queue = "ROUTE", "routed", "gate_failures"
            flags.append("gate_failed")
    if reply is not None and not sent and mode != "ROUTE_NO_DRAFT":
        store.add_draft(case_id, reply.text, gate.failures if gate else {}, "shadow" if dv.shadow else "pending_review", now)
    return Outcome(mode, disposition, queue, h.priority, tuple(flags), sent, gate), event
