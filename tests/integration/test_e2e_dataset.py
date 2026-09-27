"""End-to-end regression over the dev set (EV-9..EV-11), no LLM calls: label oracle and recorded Qwen outputs,
plus a heavy fault-injection run. Hard gates must hold everywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from router.config import load_config
from router.evals.e2e import run_e2e_eval

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "config" / "eval"


def _report(tmp_path, overlays, understanding):
    o = tmp_path / "o.yaml"
    o.write_text(yaml.safe_dump({"paths": {"reports": str(tmp_path / "reports")}}))
    loaded = load_config([*overlays, o], root=ROOT, use_local=False, env={})
    return json.loads((run_e2e_eval(loaded, "dev", understanding) / "report.json").read_text())


@pytest.mark.parametrize("understanding", ["oracle", "replay"])
def test_e2e_meets_targets(tmp_path, understanding) -> None:
    r = _report(tmp_path, [EVAL / "e2e.yaml"], understanding)
    bad = {k: v["violations"] for k, v in r["hard_gates"].items() if v["violations"]}
    assert not bad, bad
    assert all(t["met"] for t in r["targets"].values()), r["targets"]


def test_hard_gates_hold_under_heavy_faults(tmp_path) -> None:
    r = _report(tmp_path, [EVAL / "e2e.yaml", EVAL / "chaos-heavy.yaml"], "replay")
    bad = {k: v["violations"] for k, v in r["hard_gates"].items() if v["violations"]}
    assert not bad, bad
    assert r["headline"]["faults_injected"] > 100 and r["headline"]["cases_with_failed_actions_or_send"] > 0
    assert r["headline"]["wrong_actions"] == 0
