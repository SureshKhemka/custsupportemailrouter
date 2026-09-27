"""Eval reports (EV-2): JSON + Markdown, with effective config, models, prompt versions, dataset
version, metrics vs targets, failing examples, and a comparison with the previous run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from router.config import LoadedConfig
from router.dataset.loader import load_manifest
from router.llm.prompts import load_prompt


def run_header(loaded: LoadedConfig, kind: str, split: str, mode: str) -> dict[str, Any]:
    s = loaded.settings
    steps = {}
    for name, sc in s.llm.steps.items():
        try:
            digest = load_prompt(s.paths.prompts, name, sc.prompt_version).digest
        except FileNotFoundError:
            digest = None
        steps[name] = {"provider": sc.provider, "model": sc.model, "prompt_version": sc.prompt_version,
                       "prompt_digest": digest}
    return {
        "kind": kind, "split": split, "mode": mode,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_version": load_manifest(s.paths.dataset).version,
        "llm_steps": steps, "effective_config": loaded.effective(),
    }


def previous_report(reports_dir: Path, kind: str, split: str, exclude: Path | None = None) -> dict[str, Any] | None:
    root = reports_dir / kind
    if not root.is_dir():
        return None
    candidates = sorted(p for p in root.iterdir() if p.is_dir() and p.name.endswith(f"-{split}") and p != exclude)
    for p in reversed(candidates):
        f = p / "report.json"
        if f.is_file():
            return json.loads(f.read_text())
    return None


def compare(current: dict[str, float], previous: dict[str, float] | None) -> dict[str, dict[str, float | None]]:
    out = {}
    for k, v in current.items():
        prev = (previous or {}).get(k)
        out[k] = {"current": v, "previous": prev,
                  "delta": round(v - prev, 4) if isinstance(v, int | float) and isinstance(prev, int | float) else None}
    return out


def write_report(reports_dir: Path, header: dict[str, Any], headline: dict[str, float], targets: dict[str, dict],
                 body: dict[str, Any], failures: list[dict[str, Any]], markdown: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = reports_dir / header["kind"] / f"{stamp}-{header['split']}"
    out.mkdir(parents=True, exist_ok=True)
    prev = previous_report(reports_dir, header["kind"], header["split"], exclude=out)
    comparison = compare(headline, prev["headline"] if prev else None)
    report = {**header, "headline": headline, "targets": targets, "comparison": comparison,
              "previous_run": prev["started_at"] if prev else None, **body, "failures": failures}
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    (out / "report.md").write_text(_md_header(header, headline, targets, comparison, prev) + markdown)
    return out


def _md_header(header, headline, targets, comparison, prev) -> str:
    steps = "\n".join(f"| {k} | {v['provider']} | {v['model']} | {v['prompt_version']} ({v['prompt_digest']}) |"
                      for k, v in header["llm_steps"].items())
    rows = []
    for k, v in headline.items():
        t = targets.get(k)
        status = "" if t is None else ("PASS" if t["met"] else "FAIL")
        target = "" if t is None else f"{t['op']} {t['target']}"
        d = comparison[k]["delta"]
        rows.append(f"| {k} | {_fmt(v)} | {target} | {status} | {'' if d is None else f'{d:+.3f}'} |")
    return (f"# {header['kind']} eval: {header['split']} split\n\n"
            f"- Started: {header['started_at']} · mode: {header['mode']} · dataset v{header['dataset_version']}\n"
            f"- Config fingerprint: {header['effective_config']['fingerprint']}\n"
            f"- Compared with: {prev['started_at'] if prev else 'no previous run'}\n\n"
            f"| step | provider | model | prompt |\n|---|---|---|---|\n{steps}\n\n"
            f"## Headline\n\n| metric | value | target | status | vs previous |\n|---|---|---|---|---|\n"
            + "\n".join(rows) + "\n\n")


def _fmt(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, float) else str(v)
