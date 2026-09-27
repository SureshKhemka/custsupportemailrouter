"""Versioned prompt files (CF-1): config/prompts/<step>/<version>.md with `## system` and `## user` sections.

Both sections are Jinja2 templates rendered with StrictUndefined, so a missing variable fails loudly.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

_ENV = Environment(undefined=StrictUndefined, keep_trailing_newline=False, autoescape=False)
_SECTION = re.compile(r"^##\s+(system|user)\s*$", re.M | re.I)


@dataclass(frozen=True)
class Prompt:
    step: str
    version: str
    system: str
    user: str
    digest: str  # content hash, recorded with every call

    def render(self, **variables: Any) -> tuple[str, str]:
        return (_ENV.from_string(self.system).render(**variables).strip(),
                _ENV.from_string(self.user).render(**variables).strip())


@lru_cache(maxsize=64)
def load_prompt(prompts_dir: Path, step: str, version: str) -> Prompt:
    path = prompts_dir / step / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    parts = _SECTION.split(text)
    sections = {parts[i].lower(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    if set(sections) != {"system", "user"}:
        raise ValueError(f"{path}: needs exactly one '## system' and one '## user' section")
    return Prompt(step, version, sections["system"].strip(), sections["user"].strip(),
                  hashlib.sha256(text.encode()).hexdigest()[:12])
