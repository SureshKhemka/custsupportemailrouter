"""Component eval for the understanding step (section 9.3): intents, multi-intent, calibration,
entities, escalation signals, injection, language, spam. Live (records outputs) or replay (EV-1)."""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from router.config import LoadedConfig
from router.dataset.loader import LoadedRecord, load_records
from router.decide.identity import extract_order_ids
from router.evals.report import run_header, write_report
from router.llm import LLMCall, LLMClient
from router.pipeline.intake import automated_reason
from router.pipeline.understanding import LLMUnderstander, Understanding

TONE = ("anger", "chargeback_threat", "public_complaint_threat")
THRESHOLDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]


@dataclass
class Prediction:
    lr: LoadedRecord
    u: Understanding
    source: str  # llm | rule
    call: LLMCall | None = None


def run_understand_eval(loaded: LoadedConfig, split: str, mode: str, *, limit: int | None = None,
                        ids: list[str] | None = None, concurrency: int | None = None, recording: str = "eval",
                        progress: Callable[[int, int], None] | None = None) -> Path:
    cfg = loaded.settings
    records = [lr for lr in load_records(cfg.paths.dataset, (split,))]
    seen, unique = set(), []
    for lr in sorted(records, key=lambda r: r.record.id):  # a re-delivered duplicate is the same text: once
        if lr.email.message_id not in seen and (not ids or lr.record.id in ids):
            seen.add(lr.email.message_id)
            unique.append(lr)
    unique = unique[:limit] if limit else unique

    captured = threading.local()  # the LLM call made by the current worker thread
    llm = LLMClient(loaded, recording_mode="replay" if mode == "replay" else "record", recording_name=recording,
                    on_call=lambda c: setattr(captured, "call", c))
    understander = LLMUnderstander(llm, cfg)
    lock, done = threading.Lock(), [0]

    def one(lr: LoadedRecord) -> Prediction:
        if automated_reason(lr.email, cfg):  # the pipeline never sends these to the LLM (FR-6)
            p = Prediction(lr, Understanding(intents=("spam_or_auto",), language=lr.record.label.language), "rule")
        else:
            captured.call = None
            p = Prediction(lr, understander(lr.email), "llm", captured.call)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], len(unique))
        return p

    workers = concurrency or cfg.llm.steps["understand"].concurrency
    with ThreadPoolExecutor(max_workers=workers) as pool:
        preds = list(pool.map(one, unique))

    metrics, failures, md = compute(preds, loaded)
    # Subset runs are kept apart so they never become the baseline for a full run.
    header = run_header(loaded, "understand", f"{split}-subset" if (ids or limit) else split, mode)
    t = cfg.evals.targets
    targets = {
        "intent_macro_f1": _target(metrics["headline"]["intent_macro_f1"], ">=", t.intent_macro_f1),
        "multi_intent_exact_match": _target(metrics["headline"]["multi_intent_exact_match"], ">=", t.multi_intent_exact_match),
        "order_id_accuracy": _target(metrics["headline"]["order_id_accuracy"], ">=", t.order_id_accuracy),
        "legal_recall": _target(metrics["headline"]["legal_recall"], ">=", t.legal_recall),
        "language_accuracy": _target(metrics["headline"]["language_accuracy"], ">=", t.language_accuracy),
        "spam_precision": _target(metrics["headline"]["spam_precision"], ">=", t.spam_precision),
    }
    return write_report(cfg.paths.reports, header, metrics["headline"], targets,
                        {k: v for k, v in metrics.items() if k != "headline"}, failures, md)


def _target(value: float, op: str, target: float) -> dict[str, Any]:
    return {"op": op, "target": target, "met": value >= target}


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def compute(preds: list[Prediction], loaded: LoadedConfig) -> tuple[dict[str, Any], list[dict], str]:
    cfg = loaded.settings
    taxonomy = list(cfg.taxonomy.intents)
    tp, fp, fn = Counter(), Counter(), Counter()
    confusion: dict[str, Counter] = defaultdict(Counter)
    lang_tp = defaultdict(lambda: [0, 0, 0])
    failures: list[dict[str, Any]] = []
    multi_total = multi_exact = multi_missed = exact = 0
    oid_total = oid_ok = oid_llm_ok = amt_total = amt_ok = 0
    lang_total = lang_ok = cm_total = cm_ok = 0
    tone_counts = {k: Counter() for k in TONE}
    inj = Counter()
    legal = {"explicit": [0, 0], "implicit": [0, 0], "all": [0, 0]}
    spam_fp = spam_tp = spam_fn = 0
    routed = llm_failed = 0
    calib: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    tokens_in = tokens_out = 0
    latency: list[float] = []
    cost = 0.0

    for p in preds:
        label, u, rid = p.lr.record.label, p.u, p.lr.record.id
        gold, pred = set(label.intent_names), set(u.intents)
        reasons = []
        if u.failed:
            llm_failed += 1
            reasons.append(f"LLM failed: {u.failure}")
        for i in taxonomy:
            if i in gold and i in pred:
                tp[i] += 1
            elif i in pred:
                fp[i] += 1
            elif i in gold:
                fn[i] += 1
        for g in gold:
            if g in pred:
                confusion[g][g] += 1
            else:
                extras = pred - gold or {"<none>"}
                for x in extras:
                    confusion[g][x] += 1
        lk = label.language + ("-mixed" if label.code_mixed else "")
        lang_tp[lk][0] += len(gold & pred)
        lang_tp[lk][1] += len(pred - gold)
        lang_tp[lk][2] += len(gold - pred)
        if gold == pred:
            exact += 1
        else:
            reasons.append(f"intents gold={sorted(gold)} pred={sorted(pred)}")
        if len(label.intents) > 1:
            multi_total += 1
            multi_exact += gold == pred
            multi_missed += len(gold & pred) < len(gold)
        for d in u.details:
            calib[d.intent].append((d.confidence, d.intent in gold))
        if u.uncertain:
            routed += 1

        # entities
        gold_ids = {o for i in label.intents for o in i.entities.order_ids}
        text = f"{p.lr.email.subject}\n{p.lr.email.body}"
        pipeline_ids = set(extract_order_ids(text)) | set(u.order_ids)
        if gold_ids:
            oid_total += 1
            oid_ok += pipeline_ids == gold_ids
            oid_llm_ok += set(u.order_ids) == gold_ids
            if pipeline_ids != gold_ids:
                reasons.append(f"order ids gold={sorted(gold_ids)} pred={sorted(pipeline_ids)}")
        gold_amt = {round(a, 2) for i in label.intents for a in i.entities.amounts}
        if gold_amt:
            amt_total += 1
            got = {round(a, 2) for d in u.details for a in d.amounts}
            amt_ok += gold_amt <= got

        # language (LLM only; rule-closed mail has no language prediction)
        if p.source == "llm" and not u.failed:
            lang_total += 1
            lang_ok += u.language == label.language
            if u.language != label.language:
                reasons.append(f"language gold={label.language} pred={u.language}")
            if label.code_mixed:
                cm_total += 1
                cm_ok += u.code_mixed

        # escalation tone signals vs thresholds
        esc = cfg.escalation
        thr = {"anger": esc.anger_min_confidence, "chargeback_threat": esc.chargeback_or_public_threat_min_confidence,
               "public_complaint_threat": esc.chargeback_or_public_threat_min_confidence}
        for k in TONE:
            g, pr = k in label.escalation, getattr(u.tone, k) >= thr[k]
            tone_counts[k]["tp" if g and pr else "fp" if pr else "fn" if g else "tn"] += 1
            if g != pr:
                reasons.append(f"{k} gold={g} pred={pr} ({getattr(u.tone, k):.2f})")
        inj["tp" if label.injection and u.injection else "fp" if u.injection else "fn" if label.injection else "tn"] += 1
        if label.injection != u.injection:
            reasons.append(f"injection gold={label.injection} pred={u.injection}")

        if "legal_threat" in gold:
            kind = "explicit" if "legal_explicit" in p.lr.record.tags else "implicit"
            for k in (kind, "all"):
                legal[k][0] += "legal_threat" in pred
                legal[k][1] += 1
        is_spam_gold = gold == {"spam_or_auto"}
        is_spam_pred = "spam_or_auto" in pred and "spam_or_auto" not in u.uncertain and pred == {"spam_or_auto"}
        spam_tp += is_spam_gold and is_spam_pred
        spam_fp += is_spam_pred and not is_spam_gold
        spam_fn += is_spam_gold and not is_spam_pred
        if is_spam_pred and not is_spam_gold:
            reasons.insert(0, "REAL EMAIL WOULD BE DROPPED AS SPAM")

        if p.call:
            tokens_in += p.call.input_tokens
            tokens_out += p.call.output_tokens
            latency.append(p.call.latency_ms)
            cost += p.call.cost_usd
        if reasons:
            failures.append({"id": rid, "language": label.language, "tags": p.lr.record.tags, "reasons": reasons,
                             "subject": p.lr.email.subject, "uncertain": list(u.uncertain)})

    per_intent = {}
    for i in taxonomy:
        pr, rc, f1 = _prf(tp[i], fp[i], fn[i])
        per_intent[i] = {"precision": round(pr, 3), "recall": round(rc, 3), "f1": round(f1, 3),
                         "support": tp[i] + fn[i], "predicted": tp[i] + fp[i]}
    scored = [v["f1"] for v in per_intent.values() if v["support"] or v["predicted"]]
    macro = round(sum(scored) / len(scored), 4) if scored else 0.0
    n = len(preds)

    calibration = {}
    for i, pts in calib.items():
        calibration[i] = {str(t): {"precision": round(sum(ok for c, ok in pts if c >= t) / max(1, sum(c >= t for c, _ in pts)), 3),
                                   "kept": sum(c >= t for c, _ in pts), "total": len(pts)} for t in THRESHOLDS}
    tone = {k: dict(zip(("precision", "recall", "f1"), (round(x, 3) for x in _prf(c["tp"], c["fp"], c["fn"]))))
            | {"support": c["tp"] + c["fn"]} for k, c in tone_counts.items()}
    per_language = {k: dict(zip(("precision", "recall", "f1"), (round(x, 3) for x in _prf(*v)))) for k, v in lang_tp.items()}
    lat_sorted = sorted(latency)

    headline = {
        "records": n,
        "intent_macro_f1": macro,
        "intent_exact_set_match": round(exact / n, 4) if n else 0.0,
        "multi_intent_exact_match": round(multi_exact / multi_total, 4) if multi_total else 0.0,
        "missed_second_intent_rate": round(multi_missed / multi_total, 4) if multi_total else 0.0,
        "order_id_accuracy": round(oid_ok / oid_total, 4) if oid_total else 1.0,
        "order_id_accuracy_llm_only": round(oid_llm_ok / oid_total, 4) if oid_total else 1.0,
        "amount_extraction_accuracy": round(amt_ok / amt_total, 4) if amt_total else 1.0,
        "legal_recall": round(legal["all"][0] / legal["all"][1], 4) if legal["all"][1] else 1.0,
        "legal_recall_implicit": round(legal["implicit"][0] / legal["implicit"][1], 4) if legal["implicit"][1] else 1.0,
        "language_accuracy": round(lang_ok / lang_total, 4) if lang_total else 1.0,
        "code_mixed_detected": round(cm_ok / cm_total, 4) if cm_total else 1.0,
        "spam_precision": round(spam_tp / (spam_tp + spam_fp), 4) if spam_tp + spam_fp else 1.0,
        "spam_recall": round(spam_tp / (spam_tp + spam_fn), 4) if spam_tp + spam_fn else 1.0,
        "injection_recall": round(inj["tp"] / (inj["tp"] + inj["fn"]), 4) if inj["tp"] + inj["fn"] else 1.0,
        "injection_false_positives": inj["fp"],
        "share_routed_uncertain": round(routed / n, 4) if n else 0.0,
        "llm_failures": llm_failed,
        "latency_p50_s": round(lat_sorted[len(lat_sorted) // 2] / 1000, 1) if lat_sorted else 0.0,
        "latency_p95_s": round(lat_sorted[int(len(lat_sorted) * 0.95) - 1] / 1000, 1) if lat_sorted else 0.0,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": round(cost, 4),
    }
    metrics = {"headline": headline, "per_intent": per_intent,
               "confusion": {g: dict(c) for g, c in confusion.items()}, "calibration": calibration,
               "tone": tone, "legal": legal, "per_language": per_language}
    return metrics, failures, _markdown(metrics, failures)


def _markdown(m: dict[str, Any], failures: list[dict]) -> str:
    out = ["## Per intent\n", "| intent | precision | recall | F1 | support | predicted |", "|---|---|---|---|---|---|"]
    out += [f"| {i} | {v['precision']} | {v['recall']} | {v['f1']} | {v['support']} | {v['predicted']} |"
            for i, v in m["per_intent"].items()]
    out += ["\n## Confusion (gold → predicted, missed intents only)\n"]
    out += [f"- **{g}** → " + ", ".join(f"{p}: {n}" for p, n in sorted(c.items(), key=lambda x: -x[1]) if p != g)
            for g, c in m["confusion"].items() if any(p != g for p in c)]
    out += ["\n## Escalation tone signals\n", "| signal | precision | recall | F1 | support |", "|---|---|---|---|---|"]
    out += [f"| {k} | {v['precision']} | {v['recall']} | {v['f1']} | {v['support']} |" for k, v in m["tone"].items()]
    out += [f"\nLegal threats detected: explicit {m['legal']['explicit'][0]}/{m['legal']['explicit'][1]}, "
            f"implicit {m['legal']['implicit'][0]}/{m['legal']['implicit'][1]}\n"]
    out += ["## Per language (intent micro P/R/F1)\n", "| language | precision | recall | F1 |", "|---|---|---|---|"]
    out += [f"| {k} | {v['precision']} | {v['recall']} | {v['f1']} |" for k, v in sorted(m["per_language"].items())]
    out += ["\n## Calibration (precision of predictions at or above each confidence)\n",
            "| intent | " + " | ".join(str(t) for t in THRESHOLDS) + " |", "|---|" + "---|" * len(THRESHOLDS)]
    for i, rows in sorted(m["calibration"].items()):
        out.append(f"| {i} | " + " | ".join(f"{rows[str(t)]['precision']} ({rows[str(t)]['kept']}/{rows[str(t)]['total']})"
                                            for t in THRESHOLDS) + " |")
    out += [f"\n## Failing examples ({len(failures)})\n"]
    out += [f"- **{f['id']}** ({f['language']}) {f['subject']!r}: " + "; ".join(f["reasons"]) for f in failures]
    return "\n".join(out) + "\n"
