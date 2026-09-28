"""EV-12 consistency: understand the same emails several times with live model calls and report how
often each component's output changes (intents, language, tone signals, order ids, injection, and
the case decision those would lead to)."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from router.config import LoadedConfig
from router.dataset.loader import load_records
from router.decide.routing import CaseInput, IntentInput, decide_case
from router.evals.report import run_header, write_report
from router.llm import LLMClient
from router.pipeline.understanding import LLMUnderstander, Understanding

COMPONENTS = ("intents", "language", "tone_signals", "order_ids", "injection", "uncertain", "case_mode")


def _signature(u: Understanding, loaded: LoadedConfig) -> dict[str, Any]:
    cfg = loaded.settings
    esc = cfg.escalation
    tone = tuple(sorted(k for k, thr in (("anger", esc.anger_min_confidence),
                                         ("chargeback_threat", esc.chargeback_or_public_threat_min_confidence),
                                         ("public_complaint_threat", esc.chargeback_or_public_threat_min_confidence))
                        if getattr(u.tone, k) >= thr))
    mode = decide_case(CaseInput(intents=tuple(IntentInput(i) for i in u.intents),
                                 language_supported=u.language in cfg.app.supported_languages,
                                 signals=frozenset(tone), injection=u.injection, uncertain=bool(u.uncertain),
                                 understanding_failed=u.failed), cfg).mode
    return {"intents": tuple(sorted(u.intents)), "language": u.language, "tone_signals": tone,
            "order_ids": tuple(sorted(u.order_ids)), "injection": u.injection, "uncertain": tuple(sorted(u.uncertain)),
            "case_mode": mode}


def run_consistency_eval(loaded: LoadedConfig, split: str = "dev", *, runs: int | None = None, limit: int = 20,
                         ids: list[str] | None = None, concurrency: int | None = None, progress=None) -> Path:
    cfg = loaded.settings
    runs = runs or cfg.evals.consistency_runs
    records = [r for r in load_records(cfg.paths.dataset, (split,)) if not ids or r.record.id in ids]
    # a spread of categories: every k-th record, deterministic
    step = max(1, len(records) // limit) if not ids else 1
    sample = records[::step][:limit] if not ids else records
    llm = LLMClient(loaded, recording_mode="off")  # fresh calls every time, nothing recorded
    understand = LLMUnderstander(llm, cfg)
    jobs = [(lr, i) for lr in sample for i in range(runs)]
    with ThreadPoolExecutor(concurrency or cfg.llm.steps["understand"].concurrency) as pool:
        results = list(pool.map(lambda job: (job[0].record.id, understand(job[0].email)), jobs))
        if progress:
            progress(len(results))
    by_id: dict[str, list[dict]] = {}
    failures_llm = 0
    for rid, u in results:
        failures_llm += u.failed
        by_id.setdefault(rid, []).append(_signature(u, loaded))
    changed = Counter()
    unstable = []
    for rid, sigs in by_id.items():
        diffs = [c for c in COMPONENTS if len({repr(s[c]) for s in sigs}) > 1]
        for c in diffs:
            changed[c] += 1
        if diffs:
            unstable.append({"id": rid, "reasons": [f"{c}: {sorted({repr(s[c]) for s in sigs})}" for c in diffs]})
    n = len(by_id)
    headline = {"emails": n, "runs_per_email": runs, "llm_failures": failures_llm,
                "fully_stable_share": round(1 - len(unstable) / n, 3) if n else 1.0}
    headline |= {f"{c}_change_rate": round(changed[c] / n, 3) if n else 0.0 for c in COMPONENTS}
    md = ["## Output changes across repeated runs (EV-12)\n", "| component | emails whose output changed |", "|---|---|"]
    md += [f"| {c} | {changed[c]}/{n} |" for c in COMPONENTS]
    md += ["\n## Unstable emails\n"] + [f"- **{u['id']}**: " + "; ".join(u["reasons"]) for u in unstable]
    header = run_header(loaded, "consistency", split + ("-subset" if ids else ""), "live")
    return write_report(cfg.paths.reports, header, headline, {}, {"by_email": {k: [repr(s) for s in v] for k, v in by_id.items()}},
                        unstable, "\n".join(md) + "\n")
