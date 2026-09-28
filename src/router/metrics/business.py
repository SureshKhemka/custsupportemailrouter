"""Business metrics (section 9.6, BM-1..BM-8), computed only from the system's own records.

Works on any case store: a real run (`router process` + review) or an eval run. Labels are optional;
when given they add BM-7 (wrong actions against ground truth).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from router.config.models import Settings
from router.store.db import EmailRow, Store

HUMAN_DONE = {"human_replied", "closed_no_reply"}


@dataclass
class CaseRecord:
    case: dict[str, Any]
    emails: list[EmailRow]
    agent_actions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    wrong_actions: int | None = None  # set by evals that know the labels (BM-7)
    group: str | None = None  # evals run each dataset group in isolation; never match across groups

    @property
    def category(self) -> str:
        intents = self.case.get("intents") or []
        return intents[0] if intents else "unknown"

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e["data"] | {"_at": e["at"]} for e in self.events if e["kind"] == kind]


def collect(store: Store) -> list[CaseRecord]:
    return [CaseRecord(c, store.case_emails(c["case_id"]), store.agent_actions(c["case_id"]),
                       store.case_actions(c["case_id"]), store.events(c["case_id"]))
            for c in store.list_cases()]


def business_metrics(records: list[CaseRecord], cfg: Settings, now: datetime) -> dict[str, Any]:
    real = [r for r in records if r.case.get("disposition") not in {"merged", None}]
    per_cat: dict[str, Counter] = defaultdict(Counter)
    gate_reasons, model_cost, model_latency = Counter(), defaultdict(float), defaultdict(list)
    cost_by_cat: dict[str, list[float]] = defaultdict(list)
    proc_by_cat: dict[str, list[float]] = defaultdict(list)
    acceptance, edit_sizes = Counter(), []
    reviewed = overrides = 0
    sla = defaultdict(Counter)
    recontact_num = recontact_den = 0
    handled_keys: list[tuple] = []

    for r in real:
        cat, c = r.category, r.case
        per_cat[cat]["total"] += 1
        replies = r.of("reply")
        if any(e.get("sent") and e.get("final_mode") == "AUTO" for e in replies):
            per_cat[cat]["auto_sent"] += 1
        if any(e.get("shadow") and e.get("final_mode") == "AUTO" for e in replies):
            per_cat[cat]["would_auto"] += 1
        if c.get("stage") == "awaiting_human" or c.get("resolution") in HUMAN_DONE:
            per_cat[cat]["to_human"] += 1

        # BM-2 / BM-3: what humans did with drafts and decisions
        acts = r.agent_actions
        if acts:
            reviewed += 1
            overrides += any(a["decision_changed"] for a in acts)
            final = [a for a in acts if a["action"] in {"approve", "reject"}]
            if final:
                last = final[-1]
                kind = "rejected" if last["action"] == "reject" else (last["edit_class"] or "unknown")
                acceptance[kind] += 1
                if last["edit_size"] is not None and kind != "unchanged":
                    edit_sizes.append(last["edit_size"])

        # BM-5: SLA compliance by category
        if c.get("sla_due"):
            due = datetime.fromisoformat(c["sla_due"])
            closed_at = _closed_at(r)
            if closed_at is not None:
                sla[cat]["met" if closed_at <= due else "breached"] += 1
            else:
                sla[cat]["breached" if now > due else "pending"] += 1

        # BM-6: gate blocks and reasons (system and human attempts)
        for e in replies:
            if e.get("gate") and "gate_failed" in (c.get("flags") or []):
                gate_reasons.update(e["gate"].keys())
        for a in acts:
            if a["action"] == "approve_blocked_by_gate":
                gate_reasons.update((a["detail"].get("gate") or {}).keys())

        # BM-8: cost and latency
        calls = r.of("llm_call")
        cost = sum(e.get("cost_usd", 0.0) for e in calls)
        cost_by_cat[cat].append(cost)
        for e in calls:
            model_cost[e["model"]] += e.get("cost_usd", 0.0)
            model_latency[e["model"]].append(e.get("latency_ms", 0.0))
        proc = sum(e.get("duration_ms", 0.0) for e in r.of("processed"))
        if proc:
            proc_by_cat[cat].append(proc)

        # BM-4 bookkeeping: every reply the customer received (automatic or human) and when
        sent_times = [datetime.fromisoformat(e["_at"]) for e in replies if e.get("sent")]
        sent_times += [datetime.fromisoformat(a["at"]) for a in acts if a["action"] == "approve"]
        if sent_times:
            handled_keys.append((c["sender"], c.get("primary_order_id"), min(sent_times), c["case_id"], r.group))

    # BM-4: after the customer got a reply, another email on the same issue (same case, or same sender
    # and order) arrived within the repeat-contact window.
    window = timedelta(days=cfg.escalation.repeat_contact.window_days)
    all_mail = [(e, r.case, r.group) for r in records for e in r.emails if e.outcome in {"new_case", "follow_up", "merged"}]
    for sender, order_id, t, case_id, group in handled_keys:
        recontact_den += 1
        recontact_num += any(g == group and t < e.received_at <= t + window and
                             (c["case_id"] == case_id or (e.sender == sender and order_id and c.get("primary_order_id") == order_id))
                             for e, c, g in all_mail)

    total = sum(v["total"] for v in per_cat.values())
    non_spam = {k: v for k, v in per_cat.items() if k != "spam_or_auto"}
    auto = sum(v["auto_sent"] for v in non_spam.values())
    denom = sum(v["total"] for v in non_spam.values())
    labelled = [r.wrong_actions for r in records if r.wrong_actions is not None]
    return {
        "cases": total,
        "BM-1_automation": {
            "overall_rate_excl_spam": round(auto / denom, 4) if denom else 0.0,
            "would_automate_in_shadow": round(sum(v["would_auto"] for v in non_spam.values()) / denom, 4) if denom else 0.0,
            "by_category": {k: {"total": v["total"], "auto_sent": v["auto_sent"], "would_auto": v["would_auto"],
                                "to_human": v["to_human"], "rate": round(v["auto_sent"] / v["total"], 3)}
                            for k, v in sorted(per_cat.items())},
        },
        "BM-2_draft_acceptance": {
            "reviewed": sum(acceptance.values()), "outcomes": dict(acceptance),
            "unchanged_rate": round(acceptance["unchanged"] / sum(acceptance.values()), 3) if acceptance else None,
            "mean_edit_size": round(mean(edit_sizes), 3) if edit_sizes else None},
        "BM-3_override_rate": round(overrides / reviewed, 3) if reviewed else None,
        "BM-4_recontact_rate": round(recontact_num / recontact_den, 3) if recontact_den else None,
        "BM-5_sla": {"by_category": {k: dict(v) | {"compliance": round(v["met"] / (v["met"] + v["breached"]), 3)
                                                    if v["met"] + v["breached"] else None} for k, v in sorted(sla.items())},
                     "overall_compliance": _ratio(sum(v["met"] for v in sla.values()),
                                                  sum(v["met"] + v["breached"] for v in sla.values()))},
        "BM-6_gate": {"blocks": sum(gate_reasons.values()), "by_check": dict(gate_reasons),
                      "block_rate": round(sum(1 for r in real if "gate_failed" in (r.case.get("flags") or [])) / total, 4)
                      if total else 0.0},
        "BM-7_wrong_actions": sum(labelled) if labelled else None,
        "BM-8_cost_latency": {
            "cost_usd_total": round(sum(model_cost.values()), 4),
            "cost_usd_per_case": round(sum(model_cost.values()) / total, 6) if total else 0.0,
            "by_category": {k: {"cost_usd_avg": round(mean(v), 6), "processing_ms_median": round(median(proc_by_cat[k]), 1)
                                if proc_by_cat[k] else None} for k, v in sorted(cost_by_cat.items())},
            "by_model": {m: {"cost_usd": round(model_cost[m], 4), "calls": len(model_latency[m]),
                             "latency_ms_median": round(median(model_latency[m]), 1)} for m in model_latency},
        },
    }


def _closed_at(r: CaseRecord) -> datetime | None:
    if r.case.get("status") != "closed":
        return None
    approvals = [a for a in r.agent_actions if a["action"] in {"approve", "reject"}]
    if approvals:
        return datetime.fromisoformat(approvals[-1]["at"])
    sent = [e for e in r.of("reply") if e.get("sent")]
    if sent:
        return datetime.fromisoformat(sent[-1]["_at"])
    return datetime.fromisoformat(r.case["updated_at"])


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def render_markdown(bm: dict[str, Any], simulated_agent: bool = False) -> str:
    a = bm["BM-1_automation"]
    out = ["## Business metrics (section 9.6)\n"]
    if simulated_agent:
        out.append("_Agent actions below come from the **simulated agent** (label-driven), not real people._\n")
    out += [f"- **BM-1** automation rate (excl. spam): {a['overall_rate_excl_spam']}; would automate in shadow: "
            f"{a['would_automate_in_shadow']}",
            f"- **BM-2** draft acceptance: {bm['BM-2_draft_acceptance']['outcomes'] or 'no reviews'}; "
            f"mean edit size {bm['BM-2_draft_acceptance']['mean_edit_size']}",
            f"- **BM-3** human override rate: {bm['BM-3_override_rate']}",
            f"- **BM-4** recontact rate: {bm['BM-4_recontact_rate']}",
            f"- **BM-5** SLA compliance: {bm['BM-5_sla']['overall_compliance']}",
            f"- **BM-6** gate blocks: {bm['BM-6_gate']['blocks']} {bm['BM-6_gate']['by_check'] or ''} "
            f"(block rate {bm['BM-6_gate']['block_rate']})",
            f"- **BM-7** wrong actions: {bm['BM-7_wrong_actions'] if bm['BM-7_wrong_actions'] is not None else 'n/a (no labels)'}",
            f"- **BM-8** LLM cost total ${bm['BM-8_cost_latency']['cost_usd_total']} "
            f"(${bm['BM-8_cost_latency']['cost_usd_per_case']}/case); by model: {bm['BM-8_cost_latency']['by_model']}\n",
            "| category | total | auto sent | would auto (shadow) | to human | automation rate | SLA compliance |",
            "|---|---|---|---|---|---|---|"]
    sla = bm["BM-5_sla"]["by_category"]
    for k, v in a["by_category"].items():
        out.append(f"| {k} | {v['total']} | {v['auto_sent']} | {v['would_auto']} | {v['to_human']} | {v['rate']} | "
                   f"{(sla.get(k) or {}).get('compliance', '-')} |")
    return "\n".join(out) + "\n"
