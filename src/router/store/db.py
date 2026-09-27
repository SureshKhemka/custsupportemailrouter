"""Case store and audit log (FR-39, FR-40, FR-41, CF-6). SQLite, one file.

- `events` is append-only: triggers reject UPDATE and DELETE.
- Every JSON payload is masked for payment data before it is written.
- Times are stored as ISO-8601 strings from the injected clock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from router.core.masking import mask_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, thread_root TEXT NOT NULL, sender TEXT NOT NULL, customer_id TEXT,
    status TEXT NOT NULL, stage TEXT NOT NULL, disposition TEXT, mode TEXT, queue TEXT, priority INTEGER DEFAULT 0,
    language TEXT, sla_due TEXT, primary_order_id TEXT, intents TEXT, flags TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, case_id TEXT, sender TEXT NOT NULL,
    received_at TEXT NOT NULL, subject TEXT, source TEXT, order_ids TEXT, outcome TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_mid ON emails(message_id);
CREATE INDEX IF NOT EXISTS emails_sender ON emails(sender, received_at);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, step_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_case ON events(case_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS actions (
    key TEXT PRIMARY KEY, case_id TEXT NOT NULL, type TEXT NOT NULL, order_id TEXT NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, request TEXT, response TEXT, error TEXT, updated_at TEXT NOT NULL
);
"""

CASE_FIELDS = {"customer_id", "status", "stage", "disposition", "mode", "queue", "priority", "language", "sla_due",
               "primary_order_id", "intents", "flags"}
_JSON_FIELDS = {"intents", "flags"}


@dataclass(frozen=True)
class EmailRow:
    seq: int
    message_id: str
    case_id: str | None
    sender: str
    received_at: datetime
    subject: str | None
    order_ids: list[str]
    outcome: str


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ runs

    def record_run(self, run_id: str, at: datetime, kind: str, effective_config: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, at.isoformat(), kind, json.dumps(effective_config)))

    # ------------------------------------------------------------------ emails

    def record_email(self, *, message_id: str, case_id: str | None, sender: str, received_at: datetime,
                     subject: str | None, source: str | None, order_ids: list[str], outcome: str, at: datetime) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO emails (message_id, case_id, sender, received_at, subject, source, order_ids,"
                            " outcome, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (message_id, case_id, sender, received_at.isoformat(), subject, source,
                             json.dumps(order_ids), outcome, at.isoformat()))
            return int(cur.lastrowid)

    def first_email(self, message_id: str) -> EmailRow | None:
        row = self._conn.execute("SELECT * FROM emails WHERE message_id=? AND outcome != 'failed' ORDER BY seq LIMIT 1",
                                 (message_id,)).fetchone()
        return _email(row) if row else None

    def emails_from(self, sender: str, since: datetime) -> list[EmailRow]:
        rows = self._conn.execute("SELECT * FROM emails WHERE sender=? AND received_at>=? ORDER BY received_at, seq",
                                  (sender, since.isoformat())).fetchall()
        return [_email(r) for r in rows]

    def case_emails(self, case_id: str) -> list[EmailRow]:
        rows = self._conn.execute("SELECT * FROM emails WHERE case_id=? ORDER BY received_at, seq", (case_id,)).fetchall()
        return [_email(r) for r in rows]

    # ------------------------------------------------------------------ cases

    def create_case(self, case_id: str, *, thread_root: str, sender: str, at: datetime, stage: str = "received") -> None:
        with self.tx() as c:
            c.execute("INSERT INTO cases (case_id, thread_root, sender, status, stage, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?)", (case_id, thread_root, sender, "open", stage, at.isoformat(),
                                                   at.isoformat()))

    def update_case(self, case_id: str, at: datetime, **fields: Any) -> None:
        bad = set(fields) - CASE_FIELDS
        if bad:
            raise ValueError(f"unknown case fields {sorted(bad)}")
        values = {k: (json.dumps(v) if k in _JSON_FIELDS else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in values)
        with self.tx() as c:
            c.execute(f"UPDATE cases SET {sets}, updated_at=? WHERE case_id=?", (*values.values(), at.isoformat(), case_id))

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return _case(row) if row else None

    def list_cases(self, **where: Any) -> list[dict[str, Any]]:
        clause = " AND ".join(f"{k}=?" for k in where) or "1=1"
        rows = self._conn.execute(f"SELECT * FROM cases WHERE {clause} ORDER BY created_at", tuple(where.values()))
        return [_case(r) for r in rows.fetchall()]

    def customer_cases_since(self, customer_id: str, since: datetime) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM cases WHERE customer_id=? AND created_at>=? ORDER BY created_at",
                                  (customer_id, since.isoformat())).fetchall()
        return [_case(r) for r in rows]

    # ------------------------------------------------------------------ events (append-only)

    def append_event(self, case_id: str | None, step_id: str | None, kind: str, data: dict[str, Any], at: datetime) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO events (case_id, step_id, at, kind, data) VALUES (?,?,?,?,?)",
                            (case_id, step_id, at.isoformat(), kind, json.dumps(mask_obj(data), default=str)))
            return int(cur.lastrowid)

    def count_events(self, case_id: str) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM events WHERE case_id=?", (case_id,)).fetchone()[0])

    def events(self, case_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if case_id is not None:
            sql, args = sql + " AND case_id=?", [*args, case_id]
        if kind is not None:
            sql, args = sql + " AND kind=?", [*args, kind]
        rows = self._conn.execute(sql + " ORDER BY seq", args).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


def _email(row: sqlite3.Row) -> EmailRow:
    return EmailRow(row["seq"], row["message_id"], row["case_id"], row["sender"],
                    datetime.fromisoformat(row["received_at"]), row["subject"], json.loads(row["order_ids"] or "[]"),
                    row["outcome"])


def _case(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in _JSON_FIELDS:
        d[k] = json.loads(d[k]) if d[k] else None
    return d
