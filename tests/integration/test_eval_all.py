"""`router eval all` is deterministic (recorded outputs, no model calls) and passes on dev (NF-2, EV-1)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from router.config import load_config
from router.evals.all import run_all

ROOT = Path(__file__).resolve().parents[2]


def test_eval_all_passes_from_recordings(tmp_path) -> None:
    o = tmp_path / "o.yaml"
    o.write_text(yaml.safe_dump({"paths": {"reports": str(tmp_path / "reports")}}))
    loaded = load_config([o], root=ROOT, use_local=False, env={})
    report = json.loads((run_all(loaded, "dev") / "report.json").read_text())
    assert report["passed"], (report["hard_gate_failures"], report["target_failures"])
    replies = next(s for s in report["steps"] if s["step"] == "replies")["headline"]
    assert replies["judge_failures"] == 0  # every judge call was replayed
