"""Reply evals (EV-6..EV-8, agent summaries in 9.3).

Phase 1  deterministic pipeline over the dataset (templates only), capturing each reply and its facts.
Phase 2  LLM personalisation of the replies the config says to personalise (+ gate), and agent summaries.
Phase 3  LLM judge on final replies, on the template version of every personalised reply (so the value
         of personalisation is measured), and on summaries.
Phase 4  judge agreement with the rated replies (DS-7).
Plus deterministic fact checks against labels (EV-6).
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from mocks.app import build_app
from mocks.seed import resolve_relative
from mocks.services import SPECS
from router.config import LoadedConfig
from router.core.clock import FixedClock
from router.dataset.check import SeedView
from router.dataset.gate_facts import facts_for_record
from router.dataset.loader import LoadedRecord, load_rated_replies, load_records, reference_now
from router.dataset.schema import Fact
from router.evals.oracle import label_oracle
from router.evals.report import run_header, write_report
from router.gate import CaseFacts, run_gate
from router.gate import extract as X
from router.llm import LLMClient, LLMFailure
from router.pipeline.deliver import gate_facts
from router.pipeline.personalise import Personaliser, Summarizer, facts_payload, should_personalise
from router.pipeline.runner import Router
from router.pipeline.understanding import LLMUnderstander
from router.schemas.replies import JudgeOut, SummaryJudgeOut
from router.store.db import Store

RUBRICS = ("correctness", "completeness", "tone", "clarity", "language")


@dataclass
class Item:
    lr: LoadedRecord
    kind: str  # auto | draft | identity | clarify
    sent_auto: bool
    template: str
    facts: CaseFacts
    decisions: list[dict[str, Any]]
    human: bool
    reason: str
    final: str = ""
    personalised: bool = False
    personalise_note: str | None = None
    summary: dict | None = None
    judge_final: dict | None = None
    judge_template: dict | None = None
    judge_summary: dict | None = None
    fact_misses: list[str] = field(default_factory=list)
    fact_violations: list[str] = field(default_factory=list)


class Judge:
    def __init__(self, llm: LLMClient, loaded: LoadedConfig):
        cfg = loaded.settings
        self.llm = llm
        self.rubrics = [(r, (cfg.paths.rubrics / f"{r}.md").read_text(encoding="utf-8")) for r in cfg.evals.judge.rubrics]
        self.tone = cfg.paths.tone_guide.read_text(encoding="utf-8")

    def reply(self, reply: str, subject: str, body: str, facts: str) -> dict | None:
        try:
            return self.llm.run("judge", JudgeOut, {"rubrics": self.rubrics, "tone_guide": self.tone, "facts": facts,
                                                   "subject": subject, "body": body, "reply": reply}).parsed.model_dump()
        except LLMFailure:
            return None

    def summary(self, summary: dict, subject: str, body: str, facts: str) -> dict | None:
        text = "\n".join([summary["summary"], "Asks: " + "; ".join(summary["customer_asks"]),
                          "Key facts: " + "; ".join(summary["key_facts"]), "Risks: " + "; ".join(summary["risk_flags"]),
                          "Next step: " + summary["suggested_next_step"]])
        try:
            return self.llm.run("judge", SummaryJudgeOut, {"facts": facts, "subject": subject, "body": body,
                                                           "summary": text}, prompt_name="judge_summary").parsed.model_dump()
        except LLMFailure:
            return None


def run_replies_eval(loaded: LoadedConfig, split: str, *, understanding: str = "replay", mode: str = "live",
                     ids: list[str] | None = None, concurrency: int | None = None, progress=None) -> Path:
    cfg = loaded.settings
    now = reference_now(cfg.paths.dataset)
    tz = ZoneInfo(cfg.app.timezone)
    records = load_records(cfg.paths.dataset, (split,))
    recording = "eval" if split == "dev" else "eval-test"
    llm = LLMClient(loaded, recording_mode="replay" if mode == "replay" else "record", recording_name=recording)
    understander = label_oracle(records) if understanding == "oracle" else LLMUnderstander(
        LLMClient(loaded, recording_mode="replay", recording_name=recording), cfg)

    # ---- phase 1: deterministic pipeline, templates only
    items = _phase1(loaded, records, understander, now, ids)
    say = progress or (lambda msg: None)
    say(f"phase 1: {len(items)} replies/cases captured")
    workers = concurrency or cfg.llm.steps["compose"].concurrency

    # ---- phase 2: personalise + summarise (generation model)
    personaliser, summarizer = Personaliser(llm, cfg), Summarizer(llm, cfg)

    def p2(it: Item) -> None:
        it.final = it.template
        if it.template and should_personalise(it.kind, tuple(i["intent"] for i in it.decisions), cfg) \
                and it.lr.record.label.language in cfg.app.supported_languages:
            p = personaliser(it.template, it.lr.email, it.facts.language)
            if p.used_llm:
                g = run_gate(p.text, it.facts)
                if g.passed:
                    it.final, it.personalised = p.text, True
                else:
                    it.personalise_note = f"rejected by gate: {g.failed_checks}"
            else:
                it.personalise_note = p.note
        if it.human and cfg.summaries.enabled:
            s = summarizer(it.lr.email, it.facts, it.reason, it.decisions)
            it.summary = s.model_dump() if s else None

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(p2, items))
    say(f"phase 2 done: {sum(i.personalised for i in items)} personalised, {sum(bool(i.summary) for i in items)} summaries")

    # ---- phase 3: judge (separate model)
    judge = Judge(llm, loaded)
    if mode != "replay":
        _judge_preflight(loaded)

    def p3(it: Item) -> None:
        facts = facts_payload(it.facts, it.decisions)
        e = it.lr.email
        if it.final:
            it.judge_final = judge.reply(it.final, e.subject, e.body, facts)
            if it.personalised:
                it.judge_template = judge.reply(it.template, e.subject, e.body, facts)
        if it.summary:
            it.judge_summary = judge.summary(it.summary, e.subject, e.body, facts)

    with ThreadPoolExecutor(concurrency or cfg.llm.steps["judge"].concurrency) as pool:
        list(pool.map(p3, items))
    say("phase 3 done: judged")

    # ---- deterministic fact checks (EV-6)
    for it in items:
        _fact_check(it, now, tz)

    # ---- phase 4: judge agreement (EV-8)
    agreement = _agreement(loaded, judge, now, workers)
    say("phase 4 done: judge agreement")

    metrics, failures, md = _metrics(items, agreement, cfg)
    header = run_header(loaded, "replies", f"{split}-{understanding}" + ("-subset" if ids else ""), mode)
    t = cfg.evals.targets
    h = metrics["headline"]
    targets = {
        "reply_judge_avg": {"op": ">=", "target": t.reply_judge_avg, "met": h["reply_judge_avg"] >= t.reply_judge_avg},
        "auto_sent_below_min_score": {"op": "==", "target": 0, "met": h["auto_sent_below_min_score"] == 0},
        "agent_summary_avg": {"op": ">=", "target": t.agent_summary_avg, "met": h["agent_summary_avg"] >= t.agent_summary_avg},
        "fact_check_violations": {"op": "==", "target": 0, "met": h["fact_check_violations"] == 0},
    }
    return write_report(cfg.paths.reports, header, h, targets, {k: v for k, v in metrics.items() if k != "headline"},
                        failures, md)


class JudgeUnavailable(RuntimeError):
    pass


def _judge_preflight(loaded: LoadedConfig) -> None:
    """One tiny call so a judge model that cannot load stops the eval instead of scoring everything 0."""
    from router.llm.providers import ProviderError, make_provider

    sc = loaded.settings.llm.steps["judge"]
    provider = make_provider(loaded.settings.llm.providers[sc.provider])
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    try:
        provider.complete(sc.model_copy(update={"max_tokens": 20}), "Reply with JSON.", '{"ok": true}', schema, "ping")
    except ProviderError as exc:
        raise JudgeUnavailable(
            f"judge model {sc.model!r} is not available: {exc}. Generated replies and summaries are recorded; "
            "free memory (e.g. `lms unload <generation model>`) or point llm.steps.judge elsewhere, then rerun: "
            "only the judge calls will run.") from exc


# --------------------------------------------------------------------------- phase 1


def _phase1(loaded: LoadedConfig, records: list[LoadedRecord], understander, now: datetime,
            ids: list[str] | None) -> list[Item]:
    clients = {n: TestClient(build_app(n, loaded, persist=False)) for n in SPECS}
    groups: dict[str, list[LoadedRecord]] = defaultdict(list)
    wanted = {r.record.group_id for r in records if not ids or r.record.id in ids}
    for lr in sorted(records, key=lambda r: (r.email.received_at, r.record.id)):
        if lr.record.group_id in wanted:
            groups[lr.record.group_id].append(lr)
    items = []
    for group in groups.values():
        for c in clients.values():
            c.post("/_admin/reset").raise_for_status()
        router = Router(loaded, Store(":memory:"), FixedClock(now), understander=understander, http_clients=clients)
        for lr in group:
            ctx = router.process_email(lr.email)
            if ctx.outcome is None or ctx.decision is None:  # duplicate, merged, automated or crashed: no reply
                continue
            out = ctx.outcome
            human = out.disposition in {"drafted", "routed"}
            if ctx.reply is None and not human:
                continue
            facts = ctx.facts or gate_facts(ctx.understanding, ctx.identity, ctx.decision, ctx.actions, ctx.email, "",
                                            router.backends, loaded.settings, ctx.now)
            decisions = [{"intent": i.intent, "order_id": i.order_id, "decision": i.decision} for i in ctx.decision.intents]
            items.append(Item(lr, ctx.reply.kind if ctx.reply else "none", out.sent,
                              ctx.reply.text if ctx.reply else "", facts, decisions, human,
                              f"mode {out.mode}, queue {out.queue}, flags {list(out.flags)}"))
    return items


# --------------------------------------------------------------------------- EV-6 fact checks


def _present(f: Fact, text: str, now: datetime, tz: ZoneInfo) -> bool:
    low = text.lower()
    if f.kind == "amount":
        return any(abs(a - float(f.value)) < 0.01 for a in X.amounts(text))
    if f.kind == "date":
        target = datetime.fromisoformat(resolve_relative(str(f.value), now)).astimezone(tz).date()
        return any(d == target for d, _ in X.dates(text, target.year))
    return str(f.value).lower() in low


def _fact_check(it: Item, now: datetime, tz: ZoneInfo) -> None:
    r = it.lr.record.label.reply
    text = it.final
    if not text:
        return
    it.fact_misses = [f"{f.kind}={f.value}" for f in r.must_contain if not _present(f, text, now, tz)]
    it.fact_violations = [f"{f.kind}={f.value}" for f in r.must_not_contain if _present(f, text, now, tz)]


# --------------------------------------------------------------------------- EV-8 agreement


def _agreement(loaded: LoadedConfig, judge: Judge, now: datetime, workers: int) -> dict[str, Any]:
    cfg = loaded.settings
    recs = {r.record.id: r for r in load_records(cfg.paths.dataset, ("dev",))}
    seed = SeedView.load(cfg.paths.seed, now)
    rated = load_rated_replies(cfg.paths.dataset)

    def one(r):
        lr = recs[r.record]
        facts = facts_payload(facts_for_record(lr, seed, cfg), [{"intent": i.intent, "order_id": i.order_id,
                                                                  "decision": i.decision} for i in lr.record.label.intents])
        return r, judge.reply(r.reply, lr.email.subject, lr.email.body, facts)

    with ThreadPoolExecutor(workers) as pool:
        pairs = list(pool.map(one, rated))
    per: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r, j in pairs:
        if j is None:
            continue
        for k in RUBRICS:
            per[k].append((r.ratings[k], j[k]["score"]))
        per["overall"].append((r.overall, j["overall"]["score"]))
    stats = {k: {"within_1": round(sum(abs(a - b) <= 1 for a, b in v) / len(v), 3),
                 "exact": round(sum(a == b for a, b in v) / len(v), 3),
                 "mae": round(mean(abs(a - b) for a, b in v), 2), "n": len(v)} for k, v in per.items() if v}
    rater = "human" if not any(r.is_draft for r in rated) else "Claude reviewer (not human, D-049)"
    reliable = bool(stats) and stats["overall"]["within_1"] >= cfg.evals.judge.min_human_agreement
    detail = [{"id": r.id, "rated": r.overall, "judge": j["overall"]["score"] if j else None,
               "judge_reason": j["overall"]["reason"] if j else "judge failed"} for r, j in pairs]
    return {"rater": rater, "stats": stats, "reliable": reliable, "detail": detail}


# --------------------------------------------------------------------------- metrics


def _avg(xs: list[float]) -> float:
    return round(mean(xs), 3) if xs else 0.0


def _metrics(items: list[Item], agreement: dict, cfg) -> tuple[dict, list[dict], str]:
    judged = [i for i in items if i.judge_final]
    minimum = cfg.evals.targets.reply_min_score_for_auto
    low_auto = [i for i in judged if i.sent_auto and min(i.judge_final[k]["score"] for k in RUBRICS) < minimum]
    pers = [i for i in items if i.personalised and i.judge_final and i.judge_template]
    summaries = [i for i in items if i.judge_summary]
    headline = {
        "replies": sum(bool(i.final) for i in items),
        "auto_sent": sum(i.sent_auto for i in items),
        "personalised": sum(i.personalised for i in items),
        "personalisation_fallbacks": sum(bool(i.personalise_note) for i in items),
        "reply_judge_avg": _avg([i.judge_final["overall"]["score"] for i in judged]),
        "auto_reply_judge_avg": _avg([i.judge_final["overall"]["score"] for i in judged if i.sent_auto]),
        "draft_judge_avg": _avg([i.judge_final["overall"]["score"] for i in judged if not i.sent_auto]),
        "auto_sent_below_min_score": len(low_auto),
        "personalised_vs_template_delta": round(_avg([i.judge_final["overall"]["score"] for i in pers])
                                                - _avg([i.judge_template["overall"]["score"] for i in pers]), 3),
        "fact_check_pass_rate": round(sum(not i.fact_misses and not i.fact_violations for i in items if i.final)
                                      / max(1, sum(bool(i.final) for i in items)), 4),
        "fact_check_misses": sum(len(i.fact_misses) for i in items),
        "fact_check_violations": sum(len(i.fact_violations) for i in items),
        "agent_summary_avg": _avg([i.judge_summary["overall"]["score"] for i in summaries]),
        "summary_failures": sum(1 for i in items if i.human and i.summary is None),
        "judge_failures": sum(1 for i in items if i.final and i.judge_final is None),
        "judge_agreement_within_1": agreement["stats"].get("overall", {}).get("within_1", 0.0),
        "judge_reliable": agreement["reliable"],
    }
    per_rubric = {k: _avg([i.judge_final[k]["score"] for i in judged]) for k in RUBRICS}
    failures = []
    for i in items:
        reasons = []
        if i.fact_misses:
            reasons.append(f"missing facts {i.fact_misses}")
        if i.fact_violations:
            reasons.append(f"FORBIDDEN facts present {i.fact_violations}")
        if i.judge_final and i.judge_final["overall"]["score"] <= 3:
            low = {k: i.judge_final[k]["score"] for k in RUBRICS if i.judge_final[k]["score"] <= 3}
            reasons.append(f"judge {i.judge_final['overall']['score']}/5 {low}: {i.judge_final['overall']['reason']}")
        if i.personalise_note:
            reasons.append(i.personalise_note[:160])
        if i.judge_summary and i.judge_summary["overall"]["score"] <= 3:
            reasons.append(f"summary {i.judge_summary['overall']['score']}/5: {i.judge_summary['overall']['reason']}")
        if reasons:
            failures.append({"id": i.lr.record.id, "kind": i.kind, "auto": i.sent_auto, "reasons": reasons,
                             "reply": i.final})
    metrics = {"headline": headline, "per_rubric": per_rubric, "agreement": agreement}
    md = ["## Judge scores by rubric (final replies)\n", "| rubric | average |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in per_rubric.items()]
    md += [f"\n## Judge agreement (EV-8) — rater: {agreement['rater']}\n",
           f"Judge reliable (overall within-1 ≥ {cfg.evals.judge.min_human_agreement}): **{agreement['reliable']}**\n",
           "| rubric | within ±1 | exact | MAE | n |", "|---|---|---|---|---|"]
    md += [f"| {k} | {v['within_1']} | {v['exact']} | {v['mae']} | {v['n']} |" for k, v in agreement["stats"].items()]
    md += [f"\n## Findings ({len(failures)})\n"]
    md += [f"- **{f['id']}** ({f['kind']}{', auto-sent' if f['auto'] else ''}): " + "; ".join(f["reasons"]) for f in failures]
    return metrics, failures, "\n".join(md) + "\n"
