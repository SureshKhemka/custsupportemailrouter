"""Config loading, layering and validation (CF-1..CF-6, FR-19)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from router.config import ConfigError, load_config, missing_secrets
from router.config.loader import OVERLAYS_ENV, REDACTED, deep_merge

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO_CONFIG, tmp_path / "config", ignore=shutil.ignore_patterns("local.yaml"))
    return tmp_path


def overlay(root: Path, data: dict, name: str = "overlay.yaml") -> Path:
    path = root / name
    path.write_text(yaml.safe_dump(data))
    return path


def load(root: Path, *overlays: Path, env: dict | None = None):
    return load_config(overlays, root=root, env=env or {})


def problems(root: Path, data: dict) -> str:
    with pytest.raises(ConfigError) as exc:
        load(root, overlay(root, data))
    return "\n".join(exc.value.problems)


# --------------------------------------------------------------------------- happy path / layering


def test_base_config_is_valid(root: Path) -> None:
    cfg = load(root).settings
    assert cfg.app.supported_languages == ["en"]
    assert cfg.routing.matrix["legal_threat"].mode == "ROUTE_NO_DRAFT"
    assert cfg.policy.returns.window_days == 30
    assert cfg.paths.inbox == root / "var/inbox"


def test_step_defaults_apply_and_step_overrides_win(root: Path) -> None:
    llm = load(root).settings.llm
    assert llm.steps["understand"].temperature == 0.0
    assert llm.steps["compose"].temperature == 0.3
    assert llm.steps["understand"].timeout_s == llm.defaults["timeout_s"]


def test_deep_merge_merges_dicts_and_replaces_lists() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    merged = deep_merge(base, {"a": {"y": [3]}, "c": 2})
    assert merged == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}
    assert base["a"]["y"] == [1, 2]  # input not mutated


def test_layer_order_local_then_env_then_explicit(root: Path) -> None:
    (root / "config" / "local.yaml").write_text(yaml.safe_dump({"intake": {"near_duplicate_window_minutes": 10}}))
    env_layer = overlay(root, {"intake": {"near_duplicate_window_minutes": 20}}, "env.yaml")
    explicit = overlay(root, {"intake": {"near_duplicate_window_minutes": 30}}, "explicit.yaml")

    assert load(root).settings.intake.near_duplicate_window_minutes == 10
    assert load(root, env={OVERLAYS_ENV: str(env_layer)}).settings.intake.near_duplicate_window_minutes == 20
    cfg = load(root, explicit, env={OVERLAYS_ENV: str(env_layer)})
    assert cfg.settings.intake.near_duplicate_window_minutes == 30
    assert [p.name for p in cfg.layers][-3:] == ["local.yaml", "env.yaml", "explicit.yaml"]


def test_local_yaml_can_be_skipped(root: Path) -> None:
    (root / "config" / "local.yaml").write_text(yaml.safe_dump({"intake": {"near_duplicate_window_minutes": 10}}))
    cfg = load_config(root=root, use_local=False, env={})
    assert cfg.settings.intake.near_duplicate_window_minutes == 60


def test_eval_overlay_switches_provider_without_code(root: Path) -> None:
    cfg = load(root, root / "config/eval/anthropic.yaml").settings
    assert cfg.llm.steps["understand"].provider == "anthropic"
    assert cfg.llm.steps["understand"].max_tokens == 1024  # defaults still applied


# --------------------------------------------------------------------------- FR-19 invariants


@pytest.mark.parametrize("intent", ["billing_dispute", "payment_issue", "legal_threat", "abuse"])
def test_never_auto_intents_cannot_be_auto(root: Path, intent: str) -> None:
    assert "can never be AUTO" in problems(root, {"routing": {"matrix": {intent: {"mode": "AUTO"}}}})


def test_never_auto_also_checked_in_conditions(root: Path) -> None:
    msg = problems(root, {"routing": {"matrix": {"billing_dispute": {"conditions": {"not_eligible": "AUTO"}}}}})
    assert "billing_dispute" in msg and "can never be AUTO" in msg


@pytest.mark.parametrize("intent", ["legal_threat", "abuse"])
@pytest.mark.parametrize("mode", ["DRAFT", "ROUTE"])
def test_legal_and_abuse_can_never_be_drafted(root: Path, intent: str, mode: str) -> None:
    assert "can never be drafted" in problems(root, {"routing": {"matrix": {intent: {"mode": mode}}}})


def test_close_only_for_spam(root: Path) -> None:
    msg = problems(root, {"routing": {"matrix": {"complaint": {"mode": "CLOSE", "queue": None}}}})
    assert "cannot be CLOSE" in msg


def test_matrix_must_cover_every_taxonomy_intent(root: Path) -> None:
    cfg = yaml.safe_load((root / "config/routing_matrix.yaml").read_text())
    del cfg["routing"]["matrix"]["complaint"]
    (root / "config/routing_matrix.yaml").write_text(yaml.safe_dump(cfg))
    assert "missing ['complaint']" in problems(root, {})


def test_required_intents_cannot_be_removed(root: Path) -> None:
    cfg = yaml.safe_load((root / "config/taxonomy.yaml").read_text())
    del cfg["taxonomy"]["intents"]["legal_threat"]
    (root / "config/taxonomy.yaml").write_text(yaml.safe_dump(cfg))
    assert "missing required intents ['legal_threat']" in problems(root, {})


# --------------------------------------------------------------------------- CF-4 validation


def test_unknown_key_is_rejected(root: Path) -> None:
    assert "policy.returns.window_dayz" in problems(root, {"policy": {"returns": {"window_dayz": 30}}})


def test_threshold_out_of_range(root: Path) -> None:
    assert "taxonomy.intents.order_status.threshold" in problems(
        root, {"taxonomy": {"intents": {"order_status": {"threshold": 1.5}}}}
    )


def test_undefined_queue(root: Path) -> None:
    assert "undefined queues" in problems(root, {"routing": {"matrix": {"complaint": {"queue": "nowhere"}}}})


def test_step_with_unknown_provider(root: Path) -> None:
    assert "not defined in llm.providers" in problems(root, {"llm": {"steps": {"judge": {"provider": "nope"}}}})


def test_unknown_condition(root: Path) -> None:
    assert "unknown conditions" in problems(root, {"routing": {"matrix": {"order_status": {"conditions": {"tuesday": "ROUTE"}}}}})


def test_missing_section(root: Path) -> None:
    (root / "config/policy.yaml").write_text("{}\n")
    assert "policy: Field required" in problems(root, {})


def test_sla_for_unknown_intent(root: Path) -> None:
    assert "sla.per_intent refers to intents not in the taxonomy" in problems(root, {"sla": {"per_intent": {"nope": 2}}})


def test_unsupported_language_code_format(root: Path) -> None:
    assert "ISO 639-1" in problems(root, {"app": {"supported_languages": ["English"]}})


def test_invalid_yaml_reports_file(root: Path) -> None:
    bad = root / "bad.yaml"
    bad.write_text("llm: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load(root, bad)


def test_missing_overlay_file(root: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load(root, root / "nope.yaml")


def test_all_problems_reported_together(root: Path) -> None:
    msg = problems(root, {"policy": {"currency": "rupees"}, "intake": {"near_duplicate_window_minutes": 0}})
    assert "policy.currency" in msg and "intake.near_duplicate_window_minutes" in msg


# --------------------------------------------------------------------------- CF-3 / CF-6 secrets


def test_secret_value_in_config_is_rejected(root: Path) -> None:
    msg = problems(root, {"llm": {"providers": {"openrouter": {"base_url": "https://x/?k=sk-or-abcdefghijklmnopqrstuv"}}}})
    assert "looks like a secret value" in msg


def test_api_key_env_must_be_a_name(root: Path) -> None:
    assert "NAME of an environment variable" in problems(
        root, {"llm": {"providers": {"anthropic": {"api_key_env": "abc123-not-a-name"}}}}
    )


def test_api_key_field_itself_is_not_allowed(root: Path) -> None:
    assert "api_key" in problems(root, {"llm": {"providers": {"anthropic": {"api_key": "x"}}}})


def test_missing_secrets_only_for_providers_in_use(root: Path) -> None:
    assert missing_secrets(load(root), env={}) == []  # LM Studio needs no key
    cfg = load(root, root / "config/eval/anthropic.yaml")
    assert missing_secrets(cfg, env={}) == ["ANTHROPIC_API_KEY (provider 'anthropic')"]
    assert missing_secrets(cfg, env={"ANTHROPIC_API_KEY": "set"}) == []


def test_effective_config_has_no_secrets_and_is_stable(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-THIS-MUST-NEVER-APPEAR-0123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    cfg = load(root, root / "config/eval/anthropic.yaml")
    eff = cfg.effective()
    dumped = json.dumps(eff)
    assert secret not in dumped
    assert str(root) not in dumped  # paths are relative to the project root
    assert eff["config"]["llm"]["providers"]["anthropic"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert eff["fingerprint"] == load(root, root / "config/eval/anthropic.yaml").effective()["fingerprint"]
    assert eff["fingerprint"] != load(root).effective()["fingerprint"]


def test_redaction_of_secret_named_keys() -> None:
    from router.config.loader import _redact

    assert _redact({"token": "abc", "nested": [{"password": "p"}], "ok": "v"}) == {
        "token": REDACTED, "nested": [{"password": REDACTED}], "ok": "v",
    }
