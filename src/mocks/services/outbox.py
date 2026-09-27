"""MS-7 Outbox: accepts outgoing replies and stores them locally (FR-33). Nothing leaves the machine.

Idempotent per key, so a replayed case can never send a second reply (NF-5).
If an outbox directory is configured, each message is also written there as JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header
from pydantic import BaseModel, Field

from mocks.base import MockService, ServiceSpec, api_error


class OutgoingMessage(BaseModel):
    case_id: str
    to: str
    subject: str
    body: str = Field(min_length=1)
    language: str
    in_reply_to: str | None = None


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    return {"messages": {}}


def register(app: FastAPI, svc: MockService) -> None:
    out_dir: Path | None = svc.extra.get("outbox_dir")

    @app.post("/messages")
    def send(msg: OutgoingMessage = Body(...), idempotency_key: str | None = Header(default=None)):
        def do() -> dict[str, Any]:
            mid = svc.next_id("OUT")
            rec = {"message_id": mid, **msg.model_dump(), "sent_at": svc.clock.now().isoformat(timespec="seconds"),
                   "idempotency_key": idempotency_key}
            svc.data["messages"][mid] = rec
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{mid}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
            return rec

        return svc.idempotent(idempotency_key, msg.model_dump(), do)

    @app.get("/messages")
    def list_messages(case_id: str | None = None, to: str | None = None) -> list[dict[str, Any]]:
        return [m for m in svc.data["messages"].values()
                if (case_id is None or m["case_id"] == case_id) and (to is None or m["to"] == to)]

    @app.get("/messages/{message_id}")
    def get(message_id: str) -> dict[str, Any]:
        m = svc.data["messages"].get(message_id)
        if not m:
            raise api_error(404, "message_not_found", f"no message {message_id}")
        return m


def clear_outbox_dir(out_dir: Path | None) -> None:
    if out_dir and out_dir.is_dir():
        for f in out_dir.glob("OUT-*.json"):
            f.unlink()


SPEC = ServiceSpec("outbox", (), initial_data, register,
                   on_reset=lambda svc: clear_outbox_dir(svc.extra.get("outbox_dir")))
