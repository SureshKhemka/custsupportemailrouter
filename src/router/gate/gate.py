"""Outbound gate (FR-31, FR-32). Every reply, automatic or human-approved, passes here before it is sent.

Each check returns the reasons it failed (empty = passed). All checks are deterministic.
The gate only blocks; it never rewrites a reply.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from lingua import Language, LanguageDetectorBuilder

from router.core.masking import has_payment_details
from router.gate import extract as X
from router.gate.facts import CaseFacts


@dataclass(frozen=True)
class GateResult:
    failures: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def failed_checks(self) -> list[str]:
        return sorted(self.failures)


# --------------------------------------------------------------------------- facts match backend


def check_facts(reply: str, f: CaseFacts) -> list[str]:
    reasons = []
    for oid in X.extract_order_ids(reply):
        if oid not in f.order_ids:
            reasons.append(f"order id {oid} is not a verified order of this case")
    known_tracking = {o.tracking_number for o in f.orders if o.tracking_number}
    for t in X.tracking_numbers(reply):
        if t not in known_tracking:
            reasons.append(f"tracking number {t} does not match backend data")
    allowed = f.allowed_amounts()
    for a in X.amounts(reply):
        if not any(abs(a - b) < 0.01 for b in allowed):
            reasons.append(f"amount {a:,.2f} is not in backend data or policy decisions")
    reasons += _check_dates(reply, f)
    reasons += _check_days_ago(reply, f)
    return reasons


_DAYS_AGO = re.compile(r"\b(\d{1,3})\s+days?\s+ago\b", re.I)


def _check_days_ago(reply: str, f: CaseFacts) -> list[str]:
    """'delivered 25 days ago' must match the real number of calendar days since delivery."""
    if f.now is None:
        return []
    today = f.now.astimezone(f.tz).date()
    delivered = {(today - d).days for d in f.dates()["delivered"]}
    reasons = []
    for sentence in X.sentences(reply):
        if "deliver" not in sentence.lower():
            continue
        for m in _DAYS_AGO.finditer(sentence):
            if int(m.group(1)) not in delivered:
                reasons.append(f"says delivered {m.group(1)} days ago; backend says {sorted(delivered) or 'not delivered'}")
    return reasons


_DATE_CONTEXT = [  # (pattern in the text before the date, kind of date it must be)
    (re.compile(r"\b(?:expected|promised|due|estimated|scheduled|arrive|arriving|reach)\b", re.I), "promised"),
    (re.compile(r"\b(?:was|were|been|got)\s+delivered\b|\bdelivered\s+on\b", re.I), "delivered"),
    (re.compile(r"\b(?:shipped|dispatched)\b", re.I), "shipped"),
    (re.compile(r"\b(?:placed|ordered)\b", re.I), "placed"),
    (re.compile(r"\bcancel", re.I), "cancelled"),
    (re.compile(r"\brefund", re.I), "refund"),
    (re.compile(r"\bpick\s*-?up\b", re.I), "pickup"),
]


def _check_dates(reply: str, f: CaseFacts) -> list[str]:
    known = f.dates()
    everything: set[date] = set().union(*known.values())
    reasons = []
    for sentence in X.sentences(reply):
        for d, pos in X.dates(sentence, default_year=_default_year(f)):
            before = sentence[:pos]
            kind = next((k for rx, k in _DATE_CONTEXT if rx.search(before)), None)
            allowed = known[kind] if kind else everything
            if d not in allowed:
                what = f"{kind} date" if kind else "date"
                reasons.append(f"{what} {d.isoformat()} does not match backend data")
    return reasons


def _default_year(f: CaseFacts) -> int:
    years = [o.placed_at.year for o in f.orders]
    return max(years) if years else date.today().year


# --------------------------------------------------------------------------- claimed actions


_CLAIMS = {
    "issue_refund": [
        re.compile(r"\brefund\w*\b[^.]*?\b(?:has|have)\s+been\s+(issued|processed|initiated|credited|completed|sent|paid|refunded)\b", re.I),
        re.compile(r"\brefund\w*\b[^.]*?\b(?:was|is\s+now)\s+(issued|processed|initiated|credited|completed|sent|paid)\b", re.I),
        re.compile(r"\bwe(?:'ve|\s+have)\s+(refunded|issued|processed|initiated|credited)\b", re.I),
        re.compile(r"\b(?:has|have)\s+been\s+(refunded|credited)\b", re.I),
    ],
    "create_return": [
        re.compile(r"\b(?:we(?:'ve|\s+have)\s+)?(?:created|set\s+up|arranged|raised|initiated|approved|authori[sz]ed)\s+(?:a|the|your)\s+return\b", re.I),
        re.compile(r"\breturn\b[^.]*?\b(?:has|have)\s+been\s+(?:created|approved|authori[sz]ed|initiated|set\s+up|arranged)\b", re.I),
    ],
    "create_replacement": [
        re.compile(r"\b(?:we(?:'ve|\s+have)\s+)?(?:shipped|sent|dispatched|created|arranged|placed)\s+(?:a|the|your)\s+(?:new\s+)?replacement\b", re.I),
        re.compile(r"\breplacement\b[^.]*?\b(?:has|have)\s+been\s+(?:shipped|sent|dispatched|created|arranged)\b", re.I),
    ],
    "cancel_order": [
        re.compile(r"\b(?:has|have)\s+been\s+cancell?ed\b|\bwas\s+cancell?ed\b|\bwe(?:'ve|\s+have)\s+cancell?ed\b", re.I),
    ],
}
_PROCESSED_WORDS = {"processed", "credited", "completed", "paid", "refunded"}


def check_claims(reply: str, f: CaseFacts) -> list[str]:
    reasons = []
    for sentence in X.sentences(reply):
        mentioned = [o for o in X.extract_order_ids(sentence) if o in f.order_ids] or sorted(f.order_ids)
        for action, patterns in _CLAIMS.items():
            m = next((p.search(sentence) for p in patterns if p.search(sentence)), None)
            if not m:
                continue
            verb = (m.group(1) if m.groups() else "") or ""
            ok, why = _claim_supported(action, verb.lower(), mentioned, X.amounts(sentence), f)
            if not ok:
                reasons.append(f"claims {action.replace('_', ' ')} but {why}: \"{sentence[:90]}\"")
    return reasons


def _claim_supported(action: str, verb: str, order_ids: list[str], amounts: list[float],
                     f: CaseFacts) -> tuple[bool, str]:
    done = [a for a in f.actions if a.type == action and a.status == "succeeded" and a.order_id in order_ids]
    if action == "issue_refund":
        refunds = [r for oid in order_ids for r in f.refunds.get(oid, []) if r.status != "failed"]
        if verb in _PROCESSED_WORDS and not any(r.status == "processed" for r in refunds) and not done:
            return False, "no refund has been processed"
        if not refunds and not done:
            return False, "no refund exists or succeeded"
        valid = {a.amount for a in done if a.amount} | {r.amount for r in refunds}
        if refunds:
            valid.add(sum(r.amount for r in refunds))
        bad = [a for a in amounts if not any(abs(a - v) < 0.01 for v in valid)]
        return (False, f"the amount {bad[0]:,.2f} differs from the recorded refund") if bad else (True, "")
    if action == "create_return":
        exists = any(f.returns.get(oid) for oid in order_ids)
        return (True, "") if done or exists else (False, "no return was created")
    if action == "cancel_order":
        already = any(o.status == "cancelled" for o in f.orders if o.order_id in order_ids)
        return (True, "") if done or already else (False, "no cancellation succeeded")
    return (True, "") if done else (False, "no replacement was created")


# --------------------------------------------------------------------------- other customers / payment data


def check_other_customers(reply: str, f: CaseFacts) -> list[str]:
    reasons = []
    me = f.customer.customer_id if f.customer else None
    others = [c for c in f.all_customers if c.customer_id != me]
    low = reply.lower()
    for c in others:
        if c.name.lower() in low:
            reasons.append(f"mentions another customer's name ({c.customer_id})")
        if any(e.lower() in low for e in c.emails):
            reasons.append(f"mentions another customer's email ({c.customer_id})")
        if c.phone and re.sub(r"\D", "", c.phone)[-10:] in re.sub(r"\D", "", reply):
            reasons.append(f"mentions another customer's phone ({c.customer_id})")
    for oid in X.extract_order_ids(reply):
        owner = f.order_owner.get(oid)
        if owner and owner != me:
            reasons.append(f"mentions {oid}, which belongs to another customer")
    name = X.greeting_name(reply)
    if name:
        allowed = {n.split()[0].lower() for n in [f.customer.name if f.customer else "", f.sender_display_name or ""] if n}
        if name.lower() not in allowed and any(c.name.split()[0].lower() == name.lower() for c in others):
            reasons.append(f"greets '{name}', which is another customer's name")
    return reasons


def check_payment_details(reply: str, f: CaseFacts) -> list[str]:
    return ["contains card number or card security details"] if has_payment_details(reply) else []


# --------------------------------------------------------------------------- language / placeholders


_LANGS = [Language.ENGLISH, Language.HINDI, Language.SPANISH, Language.GERMAN, Language.FRENCH, Language.ITALIAN,
          Language.PORTUGUESE, Language.CHINESE]


@lru_cache(maxsize=1)
def _detector():
    return LanguageDetectorBuilder.from_languages(*_LANGS).build()


def detect_language(text: str) -> str | None:
    cleaned = re.sub(r"\bORD-\d+\b|\b[A-Z]{2}\d{8,12}[A-Z]{2}\b|[₹\d,.]+", " ", text)
    lang = _detector().detect_language_of(cleaned)
    return lang.iso_code_639_1.name.lower() if lang else None


def check_language(reply: str, f: CaseFacts) -> list[str]:
    got = detect_language(reply)
    return [] if got == f.language else [f"reply language is {got or 'unknown'}, case language is {f.language}"]


def check_placeholders(reply: str, f: CaseFacts) -> list[str]:
    return [f"unfilled placeholder {p!r}" for p in X.placeholders(reply)]


# --------------------------------------------------------------------------- policy


_NEGATED = re.compile(r"\b(?:can(?:no|')t|can\s+not|cannot|unable\s+to|not\s+able\s+to|do(?:n't|\s+not)|won't|will\s+not)\s+"
                      r"(?:\w+\s+){0,2}$", re.I)
_PROMISES = [
    (re.compile(r"\bguarantee", re.I), "guarantees an outcome"),
    (re.compile(r"\b(?:compensation|goodwill|store\s+credit|voucher|coupon|discount\s+code|cashback)\b", re.I),
     "offers compensation not in policy"),
    (re.compile(r"\byou\s+(?:can|may)\s+keep\b|\bkeep\s+(?:or\s+donate|the\s+(?:item|product))\b|\bno\s+need\s+to\s+return\b", re.I),
     "lets the customer keep an item, which policy does not provide"),
    (re.compile(r"\b(?:definitely|certainly)\b[^.]*\b(?:arrive|deliver|reach)", re.I), "promises a delivery outcome"),
    (re.compile(r"\bwill\s+(?:definitely\s+)?(?:arrive|be\s+delivered|reach\s+you)\s+(?:by|before|on|tomorrow|today)\b", re.I),
     "promises a delivery date"),
    (re.compile(r"\b(?:credited|refunded|reflect|processed)\b[^.]*\b(?:by|within)\s+(?:tomorrow|today|\d+\s+(?:hours?|days?))\b", re.I),
     "promises a refund date"),
    (re.compile(r"\bby\s+tomorrow\b", re.I), "promises a date"),
    (re.compile(r"\b(?:make|made|approve[ds]?)\s+an\s+exception\b", re.I), "grants an exception to policy"),
]
_N_DAYS = re.compile(r"\b(\d{1,3})[-\s]days?\b", re.I)


def check_policy(reply: str, f: CaseFacts) -> list[str]:
    reasons = []
    for rx, why in _PROMISES:
        for m in rx.finditer(reply):
            if not _NEGATED.search(reply[max(0, m.start() - 40):m.start()]):  # "we can't guarantee" is not a promise
                reasons.append(why)
                break
    for sentence in X.sentences(reply):
        low = sentence.lower()
        for m in _N_DAYS.finditer(sentence):
            if re.match(r"\s*ago\b", sentence[m.end():], re.I):  # a fact about the past, checked by facts_match_backend
                continue
            n = int(m.group(1))
            if "return" in low and n != f.policy.returns.window_days:
                reasons.append(f"states a {n}-day return window; policy is {f.policy.returns.window_days}")
            if any(w in low for w in ("damage", "report", "wrong item")) and "return" not in low \
                    and n != f.policy.damage.report_window_days:
                reasons.append(f"states a {n}-day reporting window; policy is {f.policy.damage.report_window_days}")
    return reasons


# --------------------------------------------------------------------------- gate


GATE_CHECKS: dict[str, Callable[[str, CaseFacts], list[str]]] = {
    "facts_match_backend": check_facts,
    "claimed_actions_succeeded": check_claims,
    "no_other_customer_data": check_other_customers,
    "no_payment_details": check_payment_details,
    "language_matches": check_language,
    "no_placeholders": check_placeholders,
    "consistent_with_policy": check_policy,
}


def run_gate(reply: str, facts: CaseFacts) -> GateResult:
    failures = {name: reasons for name, check in GATE_CHECKS.items() if (reasons := check(reply, facts))}
    return GateResult(failures)
