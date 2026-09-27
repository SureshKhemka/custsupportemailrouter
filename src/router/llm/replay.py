"""Recorded LLM outputs (EV-1 replay, NF-2 determinism). One JSONL file per recording name."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class Recordings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._items[rec["key"]] = rec

    def get(self, key: str) -> dict[str, Any] | None:
        return self._items.get(key)

    def put(self, rec: dict[str, Any]) -> None:
        with self._lock:
            if rec["key"] in self._items:
                return
            self._items[rec["key"]] = rec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def __len__(self) -> int:
        return len(self._items)
