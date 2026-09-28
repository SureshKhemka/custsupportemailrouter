"""End-to-end eval (EV-9..EV-11, section 9.2 hard gates, BM-1/BM-6/BM-7).

Every labelled email goes through the whole pipeline (intake, understanding, identity, decide,
actions, reply, gate, outbox) against in-process mock services. Each dataset group runs with a
fresh router store and freshly reset mocks (D-026). Actions are read from the mocks' call logs
(EV-5). Each group is then replayed once more to prove nothing is created twice (HG-4, NF-5).
Understanding comes from labels (`oracle`, tests code only) or recorded LLM outputs (`replay`).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mocks.app import build_app
from mocks.services import SPECS
from router.config import LoadedConfig
from router.core.clock import FixedClock
from router.dataset.check import SeedView
from router.dataset.gate_facts import facts_for_record
from router.dataset.loader import LoadedRecord, load_records, reference_now
from router.dataset.schema import RecordedAction
from router.core.ids import normalize_message_id
from router.evals.agent_sim import act_on_cases
from router.evals.oracle import label_oracle
from router.metrics.business import business_metrics, collect, render_markdown
from router.review.service import ReviewService
from router.evals.report import run_header, write_report
from router.gate import run_gate
from router.llm import LLMClient
from router.pipeline.runner import Router
from router.pipeline.understanding import LLMUnderstander
from router.store.db import Store

ACTION_ENDPOINTS = [
    ("create_return", "returns", re.compile(r"^/returns$")),
    ("issue_refund", "refund", re.compile(r"^/refunds$")),
    ("create_replacement", "replacement", re.compile(r"^/replacements$")),
    ("cancel_order", "order", re.compile(r"^/orders/(?P<order_id>[^/]+)/cancel$")),
]
NEVER_AUTO = {"billing_dispute", "payment_issue", "legal_threat", "abuse"}
NEVER_DRAFT = {"legal_threat", "abuse"}


def run_e2e_eval(loaded: LoadedConfig, split: str, understanding: str = "oracle", *,
                 recording: str | None = None, ids: list[str] | None = None, progress=None,
                 simulate_agent: bool = True, kind: str = "e2e") -> Path:
    """kind="shadow": business metrics of a shadow run (nothing sent, no actions); labels are not targets."""
    cfg = loaded.settings
    now = reference_now(cfg.paths.dataset)
    dataset = load_records(cfg.paths.dataset, (split,))
    seed = SeedView.load(cfg.paths.seed, now)
    if understanding == "oracle":
        understander = label_oracle(dataset)
    else:
        llm = LLMClient(loaded, recording_mode="replay" if understanding == "replay" else "record",
                        recording_name=recording or ("eval" if split == "dev" else "eval-test"))
        understander = LLMUnderstander(llm, cfg)

    clients = {name: TestClient(build_app(name, loaded, persist=False)) for name in SPECS}
    groups: dict[str, list[LoadedRecord]] = defaultdict(list)
    for lr in sorted(dataset, key=lambda r: (r.email.received_at, r.record.id)):
        if not ids or lr.record.group_id in {r.record.group_id for r in dataset if r.record.id in ids}:
            groups[lr.record.group_id].append(lr)

    rows: list[dict[str, Any]] = []
    # BROKEN-REPLY: a sent reply with template/code artifacts (not a spec hard gate, but never acceptable)
    hard: dict[str, list[str]] = {f"HG-{i}": [] for i in (1, 2, 3, 4, 6, 7, 8, 9)} | {"EV-11": [], "BROKEN-REPLY": []}
    faults = Counter()
    case_records: list = []  # every case the run produced, for business metrics
    secrets = [v for p in cfg.llm.providers.values() if p.api_key_env and (v := os.environ.get(p.api_key_env))]
    done = 0
    for group in groups.values():
        for c in clients.values():
            c.post("/_admin/reset").raise_for_status()
        store = Store(":memory:")
        router = Router(loaded, store, FixedClock(now), understander=understander, http_clients=clients)
        for lr in group:
            before = _call_marks(clients)
            try:
                ctx = router.process_email(lr.email)
            except Exception as exc:  # EV-11: a crash is a silent drop, which the eval must surface
                hard["EV-11"].append(f"{lr.record.id}: processing crashed: {type(exc).__name__}: {exc}")
                continue
            calls = _new_calls(clients, before)
            faults["injected"] += sum(1 for c in calls if c.get("fault"))
            executed = _executed_actions(clients, before)
            row = _row(lr, ctx, executed, store)
            flags = ctx.outcome.flags if ctx.outcome else ()
            if {"action_failed", "send_failed", "backend_unavailable", "processing_error"} & set(flags):
                faults["cases_failed"] += 1
                if row["disposition"] != "routed" or ctx.outcome.sent:
                    hard["EV-11"].append(f"{lr.record.id}: failure did not end with a human ({row['disposition']})")
            rows.append(row)
        # HG-4 / NF-5: replay the whole group; nothing new may be created or sent.
        before = _call_marks(clients)
        for lr in group:
            router.process_email(lr.email)
        created = [c for c in _new_calls(clients, before) if c["method"] == "POST" and c["status"] == 201]
        if created:
            hard["HG-4"].append(f"group {group[0].record.group_id}: replay created {[(c['path']) for c in created]}")
        _hard_gates(group, rows[-len(group):], clients, store, seed, loaded, hard, secrets)
        # After the hard gates (which judge what the system did on its own), a simulated agent works
        # the human queues, so draft acceptance, overrides and SLA can be measured (BM-2, BM-3, BM-5).
        if simulate_agent and kind == "e2e":
            svc = ReviewService(store, router.backends, cfg)
            act_on_cases(svc, store, {normalize_message_id(lr.email.message_id): lr for lr in group}, now)
        for rec in collect(store):
            rec.group = group[0].record.group_id
            first = next((lr for lr in group if normalize_message_id(lr.email.message_id) in
                          {e.message_id for e in rec.emails}), None)
            if first is not None:
                allowed = {(a.type, a.order_id) for a in [*first.record.label.actions, *first.record.label.proposed_actions]}
                ran = {(a["type"], a["order_id"]) for a in rec.actions if a["status"] == "succeeded"}
                rec.wrong_actions = len(ran - allowed)
            case_records.append(rec)
        done += len(group)
        if progress:
            progress(done, len(dataset))

    metrics, failures, md = _metrics(rows, hard)
    metrics["headline"]["faults_injected"] = faults["injected"]
    metrics["headline"]["cases_with_failed_actions_or_send"] = faults["cases_failed"]
    horizon = max(lr.email.received_at for lr in dataset) + timedelta(days=1) if dataset else now
    bm = business_metrics(case_records, cfg, horizon)
    metrics["business"] = bm
    metrics["headline"]["automation_rate_excl_spam"] = bm["BM-1_automation"]["overall_rate_excl_spam"]
    metrics["headline"]["would_automate_in_shadow"] = bm["BM-1_automation"]["would_automate_in_shadow"]
    metrics["headline"]["wrong_actions_incl_human_approved"] = bm["BM-7_wrong_actions"] or 0
    md = render_markdown(bm, simulated_agent=simulate_agent and kind == "e2e") + "\n" + md
    header = run_header(loaded, kind, f"{split}-{understanding}" + ("-faults" if cfg.faults.enabled else "")
                        + ("-subset" if ids else ""), understanding)
    t = cfg.evals.targets
    # Under fault injection, dispositions are expected to shift to humans; EV-11 only requires the hard
    # gates to hold and failed cases to reach a human (checked as "EV-11" above).
    targets = {} if cfg.faults.enabled or kind == "shadow" else {
        "disposition_accuracy": {"op": ">=", "target": t.disposition_accuracy,
                                 "met": metrics["headline"]["disposition_accuracy"] >= t.disposition_accuracy}}
    targets |= {"hard_gate_failures": {"op": "==", "target": 0, "met": metrics["headline"]["hard_gate_failures"] == 0},
                "wrong_actions": {"op": "==", "target": 0, "met": metrics["headline"]["wrong_actions"] == 0},
                "wrong_actions_incl_human_approved": {  # BM-7 must be zero
                    "op": "==", "target": 0, "met": metrics["headline"]["wrong_actions_incl_human_approved"] == 0}}
    return write_report(cfg.paths.reports, header, metrics["headline"], targets,
                        {k: v for k, v in metrics.items() if k != "headline"}, failures, md)


# --------------------------------------------------------------------------- call logs


def _call_marks(clients: dict[str, TestClient]) -> dict[str, int]:
    return {n: (c.get("/_admin/calls").json() or [{"seq": 0}])[-1]["seq"] for n, c in clients.items()}


def _new_calls(clients: dict[str, TestClient], marks: dict[str, int]) -> list[dict[str, Any]]:
    out = []
    for n, c in clients.items():
        out += [{**x, "service": n} for x in c.get("/_admin/calls", params={"since": marks[n]}).json()]
    return out


def _executed_actions(clients: dict[str, TestClient], marks: dict[str, int]) -> list[tuple]:
    """Actions that took effect: 201, a replayed 200, or a 504 whose action committed anyway."""
    done = []
    for call in _new_calls(clients, marks):
        if call["method"] != "POST":
            continue
        committed = call["status"] in (200, 201) or (call["status"] == 504 and call.get("fault") == "timeout_after_commit")
        if not committed:
            continue
        for kind, service, rx in ACTION_ENDPOINTS:
            m = rx.match(call["path"])
            if call["service"] == service and m:
                body = call["body"] or {}
                oid = m.groupdict().get("order_id") or body.get("order_id")
                lines = tuple(sorted((i["line_id"], i["qty"]) for i in body.get("items", [])))
                done.append((kind, oid, lines, body.get("amount")))
    return list(dict.fromkeys(done))


# --------------------------------------------------------------------------- per record


def _row(lr: LoadedRecord, ctx, executed: list[tuple], store: Store) -> dict[str, Any]:
    label = lr.record.label
    stage = ctx.stage
    if ctx.outcome is not None:
        disposition, mode, queue, priority = (ctx.outcome.disposition, ctx.outcome.mode, ctx.outcome.queue,
                                              ctx.outcome.priority)
    else:
        disposition = {"closed_automated": "closed_spam"}.get(stage, stage)
        mode, queue, priority = ("CLOSE", None, False)
    expected = {(a.type, a.order_id, tuple(sorted((l.line_id, l.qty) for l in a.lines))) for a in label.actions}
    got = {(k, o, l) for k, o, l, _ in executed}
    return {"lr": lr, "ctx": ctx, "disposition": disposition, "mode": mode, "queue": queue, "priority": priority,
            "executed": executed, "expected_actions": expected, "got_actions": got}


def _hard_gates(group: list[LoadedRecord], rows: list[dict], clients: dict[str, TestClient], store: Store,
                seed: SeedView, loaded: LoadedConfig, hard: dict[str, list[str]], secrets: list[str]) -> None:
    cfg = loaded.settings
    outbox = list(clients["outbox"].get("/_admin/state").json()["messages"].values())  # bypasses faults
    by_case: dict[str, list[dict]] = defaultdict(list)
    for m in outbox:
        by_case[m["case_id"]].append(m)
    case_actions: dict[str, list[tuple]] = defaultdict(list)  # everything each case has done so far
    for row in rows:
        lr, ctx, label, rid = row["lr"], row["ctx"], row["lr"].record.label, row["lr"].record.id
        case_actions[ctx.case_id] += row["executed"]
        # a reply belongs to the email it answers, not to every email of the case
        sent = [m for m in by_case.get(ctx.case_id, []) if m["in_reply_to"] == lr.email.message_id]
        intents = label.intent_names
        if intents & NEVER_AUTO and sent:
            hard["HG-1"].append(f"{rid}: {sorted(intents & NEVER_AUTO)} case was auto-sent")
        if intents & NEVER_DRAFT and store.drafts(ctx.case_id):
            hard["HG-2"].append(f"{rid}: {sorted(intents & NEVER_DRAFT)} case has a draft")
        if label.ownership in {"not_owner", "unknown_sender"} and label.intent_names - {"product_question", "abuse",
                                                                                        "legal_threat", "spam_or_auto"}:
            if row["executed"]:
                hard["HG-3"].append(f"{rid}: action for unverified sender {row['executed']}")
            for m in sent:
                if re.search(r"ORD-\d{6}|\b[A-Z]{2}\d{8,12}[A-Z]{2}\b|₹", m["body"]):
                    hard["HG-3"].append(f"{rid}: order details disclosed to unverified sender")
        if label.injection and (row["disposition"] != label.case.disposition
                                or row["got_actions"] != row["expected_actions"]):
            hard["HG-8"].append(f"{rid}: injection changed the outcome ({row['disposition']}, {sorted(row['got_actions'])})")
        # HG-6 / HG-7: re-check every sent reply against seed facts and the actions that really happened.
        if sent:
            done = [RecordedAction(type=k, order_id=o, amount=a, status="succeeded")
                    for k, o, _, a in dict.fromkeys(case_actions[ctx.case_id])]
            facts = facts_for_record(lr, seed, cfg, done)
            for m in sent:
                res = run_gate(m["body"], facts)
                for check, key in (("facts_match_backend", "HG-6"), ("consistent_with_policy", "HG-6"),
                                   ("claimed_actions_succeeded", "HG-7"), ("no_placeholders", "BROKEN-REPLY")):
                    if check in res.failures:
                        hard[key].append(f"{rid}: {check}: {res.failures[check][0]}")
    blob = "\n".join([str(store.events()), str(outbox)])
    for s in secrets:
        if s and s in blob:
            hard["HG-9"].append("a configured API key appears in logs or outbox")
    if re.search(r"\bsk-(?:ant-|or-)?[A-Za-z0-9_\-]{16,}", blob):
        hard["HG-9"].append("a secret-looking key appears in logs or outbox")


# --------------------------------------------------------------------------- metrics


def _metrics(rows: list[dict], hard: dict[str, list[str]]) -> tuple[dict, list[dict], str]:
    n = len(rows)
    ok_disp = ok_mode = ok_queue = ok_actions = 0
    wrong_actions, forbidden = [], []
    auto_by_cat, total_by_cat = Counter(), Counter()
    gate_blocks = Counter()
    failures = []
    for r in rows:
        lr, label, rid = r["lr"], r["lr"].record.label, r["lr"].record.id
        reasons = []
        if r["disposition"] == label.case.disposition:
            ok_disp += 1
        else:
            reasons.append(f"disposition {r['disposition']} (label {label.case.disposition})")
        if r["mode"] == label.case.mode:
            ok_mode += 1
        else:
            reasons.append(f"mode {r['mode']} (label {label.case.mode})")
        if r["queue"] == label.case.queue:
            ok_queue += 1
        elif label.case.disposition not in {"duplicate_ignored", "merged"}:
            reasons.append(f"queue {r['queue']} (label {label.case.queue})")
        if r["got_actions"] == r["expected_actions"]:
            ok_actions += 1
        else:
            reasons.append(f"actions {sorted(r['got_actions'])} (label {sorted(r['expected_actions'])})")
        extra = r["got_actions"] - r["expected_actions"]
        if extra:
            wrong_actions.append((rid, sorted(extra)))
        if {k for k, *_ in r["got_actions"]} & set(label.forbidden_actions):
            forbidden.append(rid)
        cat = label.intents[0].intent
        total_by_cat[cat] += 1
        if r["ctx"].outcome is not None and r["ctx"].outcome.sent and r["disposition"] == "auto_replied":
            auto_by_cat[cat] += 1
        flags = r["ctx"].outcome.flags if r["ctx"].outcome else ()
        if "gate_failed" in flags:
            gate = r["ctx"].outcome.gate
            for check in (gate.failed_checks if gate else ["unknown"]):
                gate_blocks[check] += 1
            reasons.append(f"gate blocked: {gate.failures if gate else ''}")
        if "action_failed" in flags:
            reasons.append("an action failed; routed to a human")
        if reasons:
            failures.append({"id": rid, "tags": lr.record.tags, "reasons": reasons})
    hg_fail = sum(len(v) for v in hard.values())
    headline = {
        "records": n,
        "disposition_accuracy": round(ok_disp / n, 4) if n else 0.0,
        "mode_accuracy": round(ok_mode / n, 4) if n else 0.0,
        "queue_accuracy": round(ok_queue / n, 4) if n else 0.0,
        "action_exact_match": round(ok_actions / n, 4) if n else 0.0,
        "wrong_actions": len(wrong_actions),
        "forbidden_action_violations": len(forbidden),
        "hard_gate_failures": hg_fail,
        "auto_sent": sum(auto_by_cat.values()),
        "automation_rate": round(sum(auto_by_cat.values()) / n, 4) if n else 0.0,
        "gate_blocks": sum(gate_blocks.values()),
    }
    metrics = {"headline": headline, "hard_gates": {k: {"passed": not v, "violations": v} for k, v in hard.items()},
               "automation_by_category": {c: {"auto": auto_by_cat[c], "total": t, "rate": round(auto_by_cat[c] / t, 3)}
                                          for c, t in sorted(total_by_cat.items())},
               "gate_blocks_by_check": dict(gate_blocks), "wrong_actions_detail": wrong_actions}
    md = ["## Hard gates\n", "| gate | result | violations |", "|---|---|---|"]
    md += [f"| {k} | {'PASS' if not v else 'FAIL'} | {'; '.join(v[:5])} |" for k, v in hard.items()]
    md += ["\n## Automation by primary intent (BM-1)\n", "| intent | auto-sent | total | rate |", "|---|---|---|---|"]
    md += [f"| {c} | {v['auto']} | {v['total']} | {v['rate']} |" for c, v in metrics["automation_by_category"].items()]
    md += [f"\nGate blocks by check (BM-6): {dict(gate_blocks) or 'none'}\n",
           f"Wrong actions (BM-7): {wrong_actions or 'none'}\n", f"## Mismatches ({len(failures)})\n"]
    md += [f"- **{f['id']}**: " + "; ".join(f["reasons"]) for f in failures]
    return metrics, failures, "\n".join(md) + "\n"
