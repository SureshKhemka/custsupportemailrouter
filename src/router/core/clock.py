"""Injectable notion of "now" (FR-38, NF-2). All datetimes are timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. Used by tests, evals and replay runs."""

    def __init__(self, at: datetime):
        self._at = as_utc(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = as_utc(at)

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}; include a UTC offset")
    return dt.astimezone(UTC)


def make_clock(fixed_now: datetime | None) -> Clock:
    return FixedClock(fixed_now) if fixed_now is not None else SystemClock()
