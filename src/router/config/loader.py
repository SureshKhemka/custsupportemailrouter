"""Load, layer, validate and snapshot configuration (CF-4, CF-5, CF-6).

Layer order: base.yaml (with its `includes`) -> config/local.yaml if present ->
ROUTER_CONFIG_OVERLAYS (os.pathsep-separated) -> explicit overlays, in order.
Dicts merge recursively; lists and scalars from later layers replace earlier ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from router.config.models import Settings

OVERLAYS_ENV = "ROUTER_CONFIG_OVERLAYS"
HOME_ENV = "ROUTER_HOME"

# Values that look like real credentials must never appear in config files (CF-3).
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-(?:ant-|or-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]
# Keys whose values are always redacted in snapshots, whatever they contain.
_SECRET_KEY_PATTERN = re.compile(r"(?i)(api_key|secret|token|password)$")
REDACTED = "***REDACTED***"


class ConfigError(Exception):
    """Configuration is missing, malformed or breaks an invariant. The system must not start."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class LoadedConfig:
    settings: Settings
    root: Path
    layers: list[Path] = field(default_factory=list)

    def effective(self) -> dict[str, Any]:
        """Effective configuration with secrets removed, for run records and reports (CF-6)."""
        return effective_config(self)


def project_root() -> Path:
    env = os.environ.get(HOME_ENV)
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_layer(path: Path, loaded: list[Path], stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    path = path.resolve()
    if path in stack:
        raise ConfigError([f"include cycle: {' -> '.join(str(p) for p in (*stack, path))}"])
    if not path.is_file():
        raise ConfigError([f"config file not found: {path}"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path}: invalid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ConfigError([f"{path}: top level must be a mapping"])

    includes = data.pop("includes", []) or []
    if not isinstance(includes, list):
        raise ConfigError([f"{path}: 'includes' must be a list of file names"])
    merged: dict[str, Any] = {}
    for inc in includes:
        merged = deep_merge(merged, _read_layer(path.parent / inc, loaded, (*stack, path)))
    loaded.append(path)
    return deep_merge(merged, data)


def _find_secret_values(node: Any, where: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, Mapping):
        for k, v in node.items():
            found += _find_secret_values(v, f"{where}.{k}" if where else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _find_secret_values(v, f"{where}[{i}]")
    elif isinstance(node, str) and any(p.search(node) for p in _SECRET_VALUE_PATTERNS):
        found.append(f"{where}: looks like a secret value; secrets must come from environment variables (CF-3)")
    return found


def _format_validation_error(exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        msg = err["msg"].removeprefix("Value error, ")
        problems.append(f"{loc}: {msg}")
    return problems


def load_config(
    overlays: Iterable[str | Path] = (),
    *,
    root: Path | None = None,
    use_local: bool = True,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load and validate configuration. Raises ConfigError with every problem found."""
    root = (root or project_root()).resolve()
    env = os.environ if env is None else env
    config_dir = root / "config"

    layer_files: list[Path] = [config_dir / "base.yaml"]
    local = config_dir / "local.yaml"
    if use_local and local.is_file():
        layer_files.append(local)
    layer_files += [Path(p) for p in env.get(OVERLAYS_ENV, "").split(os.pathsep) if p]
    layer_files += [Path(p) for p in overlays]

    loaded: list[Path] = []
    raw: dict[str, Any] = {}
    for f in layer_files:
        raw = deep_merge(raw, _read_layer(f if f.is_absolute() else root / f, loaded))

    leaks = _find_secret_values(raw)
    if leaks:
        raise ConfigError(leaks)

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None

    settings.paths = settings.paths.resolved(root)
    return LoadedConfig(settings=settings, root=root, layers=loaded)


def _redact(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: (REDACTED if _SECRET_KEY_PATTERN.search(str(k)) and v is not None else _redact(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(v) for v in node]
    if isinstance(node, str) and any(p.search(node) for p in _SECRET_VALUE_PATTERNS):
        return REDACTED
    return node


def effective_config(loaded: LoadedConfig) -> dict[str, Any]:
    body = _redact(loaded.settings.model_dump(mode="json"))
    # Paths relative to the project root keep snapshots portable and free of home-directory names.
    body["paths"] = {k: _relative(Path(v), loaded.root) for k, v in body["paths"].items()}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return {
        "fingerprint": hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "layers": [_relative(p, loaded.root) for p in loaded.layers],
        "config": body,
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def missing_secrets(loaded: LoadedConfig, env: Mapping[str, str] | None = None) -> list[str]:
    """Env vars required by providers that some LLM step uses, but which are not set."""
    env = os.environ if env is None else env
    llm = loaded.settings.llm
    used = {step.provider for step in llm.steps.values()}
    return sorted(
        f"{llm.providers[p].api_key_env} (provider '{p}')"
        for p in used
        if llm.providers[p].api_key_env and not env.get(llm.providers[p].api_key_env)
    )
