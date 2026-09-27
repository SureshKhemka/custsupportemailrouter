"""Load dataset records and materialise their emails (DS-1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from mocks.seed import resolve_relative
from router.dataset.schema import DatasetManifest, GateCase, RatedReply, Record
from router.schemas.email import InboundEmail

SPLITS = ("dev", "test")
SUPPORT_ADDRESS = "support@shop.example"


@dataclass(frozen=True)
class LoadedRecord:
    record: Record
    split: str
    source: Path
    email: InboundEmail


def load_manifest(dataset_dir: Path) -> DatasetManifest:
    return DatasetManifest.model_validate(yaml.safe_load((dataset_dir / "dataset.yaml").read_text()))


def reference_now(dataset_dir: Path) -> datetime:
    return datetime.fromisoformat(load_manifest(dataset_dir).reference_now)


def load_records(dataset_dir: Path, splits: tuple[str, ...] = SPLITS) -> list[LoadedRecord]:
    now = reference_now(dataset_dir)
    out: list[LoadedRecord] = []
    for split in splits:
        for path in sorted((dataset_dir / split).glob("*.yaml")):
            for raw in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                try:
                    rec = Record.model_validate(raw)
                    email = to_email(rec, now)
                except Exception as exc:  # re-raise with location for authors
                    raise ValueError(f"{path.name}: record {raw.get('id', '?')}: {exc}") from exc
                out.append(LoadedRecord(rec, split, path, email))
    return out


def to_email(rec: Record, now: datetime) -> InboundEmail:
    data = {"message_id": f"<{rec.id}@mail.test>", "to": SUPPORT_ADDRESS, **rec.email}
    return InboundEmail.model_validate(resolve_relative(data, now))


def write_inbox(records: list[LoadedRecord], inbox: Path) -> list[Path]:
    """Write each record's email as an inbox JSON file (docs/email-format.md)."""
    inbox.mkdir(parents=True, exist_ok=True)
    paths = []
    for lr in sorted(records, key=lambda r: (r.email.received_at, r.record.id)):
        p = inbox / f"{lr.record.id}.json"
        p.write_text(json.dumps(lr.email.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False))
        paths.append(p)
    return paths


# --------------------------------------------------------------------------- DS-6 / DS-7

_DATE_MARKER = re.compile(r"<<date:(@now(?:[+-]\d+(?:\.\d+)?d)?)>>")


def render_dates(text: str, now: datetime, tz: ZoneInfo) -> str:
    """Replace <<date:@now-28d>> with a concrete date such as '30 August 2026' (in the business timezone)."""
    def sub(m: re.Match[str]) -> str:
        dt = datetime.fromisoformat(resolve_relative(m.group(1), now)).astimezone(tz)
        return f"{dt.day} {dt:%B %Y}"
    return _DATE_MARKER.sub(sub, text)


def load_gate_cases(dataset_dir: Path, tz: ZoneInfo) -> list[GateCase]:
    now = reference_now(dataset_dir)
    raw = yaml.safe_load((dataset_dir / "bad_replies" / "bad_replies.yaml").read_text(encoding="utf-8")) or []
    return [GateCase.model_validate({**r, "reply": render_dates(r["reply"], now, tz)}) for r in raw]


def load_rated_replies(dataset_dir: Path) -> list[RatedReply]:
    raw = yaml.safe_load((dataset_dir / "human_ratings" / "ratings.yaml").read_text(encoding="utf-8")) or []
    return [RatedReply.model_validate(r) for r in raw]
