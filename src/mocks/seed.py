"""Seed loading with dates relative to an injectable "now" (MS-8, SD-6)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# "@now", "@now-29d", "@now+2.5d"
_REL = re.compile(r"^@now(?:([+-]\d+(?:\.\d+)?)d)?$")


def resolve_relative(node: Any, now: datetime) -> Any:
    """Replace every '@now±Nd' string with an absolute ISO-8601 UTC timestamp."""
    if isinstance(node, dict):
        return {k: resolve_relative(v, now) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_relative(v, now) for v in node]
    if isinstance(node, str) and (m := _REL.match(node)):
        days = float(m.group(1)) if m.group(1) else 0.0
        return (now + timedelta(days=days)).isoformat(timespec="seconds")
    return node


def load_seed_files(seed_dir: Path, names: tuple[str, ...], now: datetime) -> tuple[dict[str, Any], str]:
    """Load seed files (resolved against `now`) and a fingerprint of their raw content."""
    digest = hashlib.sha256()
    data: dict[str, Any] = {}
    for name in names:
        raw = (seed_dir / name).read_bytes()
        digest.update(name.encode() + b"\0" + raw)
        data[name.removesuffix(".json")] = resolve_relative(json.loads(raw), now)
    return data, digest.hexdigest()[:16]
