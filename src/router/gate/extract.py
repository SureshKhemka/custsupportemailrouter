"""Pull checkable facts out of reply text."""

from __future__ import annotations

import re
from datetime import date

from router.decide.identity import extract_order_ids

__all__ = ["extract_order_ids", "amounts", "tracking_numbers", "dates", "sentences", "greeting_name",
           "placeholders"]

_AMOUNT = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?:rupees|inr)\b",
    re.IGNORECASE)
_TRACKING = re.compile(r"\b[A-Z]{2}\d{8,12}[A-Z]{2}\b")
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], start=1)}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = [
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?(?:\s+(\d{{4}}))?", re.I), "dmy"),
    (re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?", re.I), "mdy"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "slash"),  # dd/mm/yyyy (Indian convention)
]
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}|\{[a-z_][a-z0-9_]*\}|\[[A-Z][A-Z _]{2,}\]|<<[^>]*>>|<[A-Z][A-Z_]{2,}>"
                          r"|\bX{3,}\b|\bTBD\b|\bINSERT\b")
_GREETING = re.compile(r"^\s*(?:hi|hello|dear|hey)\s+([A-Za-z][a-z]+)", re.I | re.M)


def amounts(text: str) -> list[float]:
    out = []
    for m in _AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        out.append(round(float(raw), 2))
    return out


def tracking_numbers(text: str) -> list[str]:
    return _TRACKING.findall(text)


def _month(tok: str) -> int:
    tok = tok.lower()
    return next(v for k, v in _MONTHS.items() if k.startswith(tok[:3]))


def dates(text: str, default_year: int) -> list[tuple[date, int]]:
    """(date, position) for every date written in the text; unparseable ones are skipped."""
    found: list[tuple[date, int]] = []
    taken: list[range] = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() in r for r in taken):
                continue
            try:
                if kind == "dmy":
                    d = date(int(m.group(3) or default_year), _month(m.group(2)), int(m.group(1)))
                elif kind == "mdy":
                    d = date(int(m.group(3) or default_year), _month(m.group(1)), int(m.group(2)))
                elif kind == "iso":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
            found.append((d, m.start()))
            taken.append(range(m.start(), m.end()))
    return sorted(found, key=lambda x: x[1])


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def greeting_name(text: str) -> str | None:
    m = _GREETING.search(text)
    return m.group(1) if m else None


def placeholders(text: str) -> list[str]:
    return _PLACEHOLDER.findall(text)
