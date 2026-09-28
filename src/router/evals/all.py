"""`router eval all`: every eval in one command, one combined report (section 9).

Default: deterministic (NF-2, EV-1) — recorded LLM outputs, no model calls. `--live` calls the
configured models (and records), and adds the EV-12 consistency run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from router.config import LoadedConfig, load_config
from router.dataset.check import SeedView, check_dataset
from router.dataset.gate_facts import facts_for_record
from router.dataset.loader import load_gate_cases, load_records, reference_now
from router.evals.report import run_header
from router.gate import run_gate

ROOT_EVAL = Path(__file__).resolve().parents[3] / "config" / "eval"


def run_all(loaded: LoadedConfig, split: str = "dev", *, live: bool = False, replies: bool = True,
            consistency: bool = False, say: Callable[[str], None] = lambda m: None) -> Path:
    from router.evals.consistency import run_consistency_eval
    from router.evals.e2e import run_e2e_eval
    from router.evals.replies import run_replies_eval
    from router.evals.understand import run_understand_eval

    cfg = loaded.settings
    mode = "live" if live else "replay"
    e2e_loaded = _with(loaded, ROOT_EVAL / "e2e.yaml")
    steps: list[dict[str, Any]] = []

    def record(name: str, report_dir: Path | None, targets: dict[str, dict] | None, hard: dict | None = None,
               headline: dict | None = None, note: str = "") -> None:
        rep = json.loads((report_dir / "report.json").read_text()) if report_dir else {}
        steps.append({"step": name, "report": str(report_dir / "report.md") if report_dir else None,
                      "targets": targets if targets is not None else rep.get("targets", {}),
                      "hard_gates": hard if hard is not None else rep.get("hard_gates", {}),
                      "headline": headline if headline is not None else rep.get("headline", {}), "note": note})

    say("DS-5 label consistency")
    res = check_dataset(load_records(cfg.paths.dataset), cfg, cfg.paths.dataset)
    record("dataset check (DS-1..DS-7)", None, {"label_problems": {"op": "==", "target": 0, "met": res.ok}},
           headline={"label_problems": len(res.problems), **{k: res.stats.get(k) for k in ("total", "by_split")}})

    say("HG-5 outbound gate vs deliberately bad replies")
    record("outbound gate (HG-5)", None, {}, hard={"HG-5": _hg5(loaded)}, headline={})

    say("understanding (9.3)")
    record("understanding", run_understand_eval(loaded, split, mode, recording="eval" if split == "dev" else "eval-test"), None)

    for name, overlays, u in [("end to end, label oracle", [], "oracle"), ("end to end, LLM", [], mode),
                              ("end to end, heavy faults (EV-11)", [ROOT_EVAL / "chaos-heavy.yaml"], mode)]:
        say(name)
        rl = _with(e2e_loaded, *overlays)
        record(name, run_e2e_eval(rl, split, u), None)

    say("shadow run (BM-1)")
    shadow = run_e2e_eval(_with(e2e_loaded, ROOT_EVAL / "shadow.yaml"), split, mode, kind="shadow", simulate_agent=False)
    rep = json.loads((shadow / "report.json").read_text())
    sent = rep["business"]["BM-1_automation"]["overall_rate_excl_spam"]
    record("shadow run", shadow, {"nothing_sent_in_shadow": {"op": "==", "target": 0, "met": sent == 0}},
           headline={"would_automate": rep["business"]["BM-1_automation"]["would_automate_in_shadow"], "sent": sent})

    if replies:
        say("replies (EV-6..EV-8)")
        try:
            record("replies", run_replies_eval(e2e_loaded, split, mode=mode), None)
        except Exception as exc:  # e.g. no recordings for this split in replay mode
            record("replies", None, {}, headline={}, note=f"skipped: {type(exc).__name__}: {exc}")
    if consistency and live:
        say("consistency (EV-12)")
        record("consistency", run_consistency_eval(loaded, split), {})

    return _write(loaded, split, mode, steps)


def _with(loaded: LoadedConfig, *overlays: Path) -> LoadedConfig:
    """The caller's config plus more overlays (base files and their includes are loaded again by load_config)."""
    base_dir = loaded.root / "config"
    caller = [p for p in loaded.layers if p.parent != base_dir]  # local.yaml and explicit overlays
    local = base_dir / "local.yaml"
    caller += [local] if local in loaded.layers else []
    return load_config([*dict.fromkeys([*caller, *overlays])], root=loaded.root, use_local=False)


def _hg5(loaded: LoadedConfig) -> dict[str, Any]:
    cfg = loaded.settings
    now = reference_now(cfg.paths.dataset)
    recs = {r.record.id: r for r in load_records(cfg.paths.dataset)}
    seed = SeedView.load(cfg.paths.seed, now)
    wrong = []
    cases = load_gate_cases(cfg.paths.dataset, ZoneInfo(cfg.app.timezone))
    for c in cases:
        res = run_gate(c.reply, facts_for_record(recs[c.record], seed, cfg, c.recorded_actions))
        if (not res.passed) != (c.expect == "block") or not set(c.checks) <= set(res.failed_checks):
            wrong.append(f"{c.id}: expected {c.expect} {c.checks}, got {res.failed_checks}")
    return {"passed": not wrong, "violations": wrong, "cases": len(cases)}


def _write(loaded: LoadedConfig, split: str, mode: str, steps: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = loaded.settings.paths.reports / "all" / f"{stamp}-{split}"
    out.mkdir(parents=True, exist_ok=True)
    hard_fail = {f"{s['step']}: {k}": v.get("violations", [])[:3] for s in steps for k, v in s["hard_gates"].items()
                 if not v.get("passed", True)}
    target_fail = {f"{s['step']}: {k}": t for s in steps for k, t in s["targets"].items() if not t.get("met", True)}
    ok = not hard_fail and not target_fail
    summary = {**run_header(loaded, "all", split, mode), "passed": ok, "hard_gate_failures": hard_fail,
               "target_failures": target_fail, "steps": steps}
    (out / "report.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    md = [f"# All evals: {split} split ({mode})\n", f"**Result: {'PASS' if ok else 'FAIL'}**\n",
          f"Config fingerprint {summary['effective_config']['fingerprint']}, dataset v{summary['dataset_version']}\n",
          "| step | targets | hard gates | report |", "|---|---|---|---|"]
    for s in steps:
        t = ", ".join(f"{k} {'✓' if v.get('met') else '✗'}" for k, v in s["targets"].items()) or "-"
        h = ", ".join(f"{k} {'✓' if v.get('passed', True) else '✗'}" for k, v in s["hard_gates"].items()) or "-"
        md.append(f"| {s['step']} | {t} | {h} | {s['report'] or s['note'] or '-'} |")
    if hard_fail or target_fail:
        md += ["\n## Failures\n"] + [f"- {k}: {v}" for k, v in {**hard_fail, **target_fail}.items()]
    (out / "report.md").write_text("\n".join(md) + "\n")
    return out
