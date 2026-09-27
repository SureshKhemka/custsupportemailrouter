"""Replay the recorded understand outputs for the dev set through the eval (EV-1): no LLM calls.

Fails if any section 9.3 target drops, or if the current prompt/model has no recording for a dev
email (re-record with `router eval understand --mode live`).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from router.config import load_config
from router.evals.understand import run_understand_eval

ROOT = Path(__file__).resolve().parents[2]


def test_understand_eval_replay_meets_targets(tmp_path) -> None:
    overlay = tmp_path / "o.yaml"
    overlay.write_text(yaml.safe_dump({"paths": {"reports": str(tmp_path / "reports")}}))
    loaded = load_config([overlay], root=ROOT, use_local=False, env={})
    out = run_understand_eval(loaded, "dev", "replay")
    report = json.loads((out / "report.json").read_text())
    assert report["headline"]["llm_failures"] == 0, "missing recordings: re-run the live eval"
    failed = {k: report["headline"][k] for k, t in report["targets"].items() if not t["met"]}
    assert not failed, failed
    assert report["headline"]["spam_precision"] == 1.0 and report["headline"]["legal_recall"] == 1.0
