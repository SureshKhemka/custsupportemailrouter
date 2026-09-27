"""Payment-data detection and masking (FR-31, FR-41, HG-9)."""

from __future__ import annotations

import re
from typing import Any

# 13-19 digits, optionally grouped by spaces or dashes.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_CVV = re.compile(r"(?i)\b(cvv|cvc|cvv2|security code)\b\s*[:#-]?\s*\d{3,4}\b")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def find_card_numbers(text: str) -> list[str]:
    found = []
    for m in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            found.append(m.group())
    return found


def has_payment_details(text: str) -> bool:
    return bool(find_card_numbers(text) or _CVV.search(text))


def mask_text(text: str) -> str:
    """Replace card numbers with ****-****-****-1234 and CVVs with ***."""
    def card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            return f"****-****-****-{digits[-4:]}"
        return m.group()
    text = _CARD_CANDIDATE.sub(card, text)
    return _CVV.sub(lambda m: f"{m.group(1)} ***", text)


def mask_obj(obj: Any) -> Any:
    """Mask every string inside a JSON-like structure (for logs and reports)."""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [mask_obj(v) for v in obj]
    return obj
