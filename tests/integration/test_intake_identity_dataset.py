"""Component eval: intake + identity over every labelled email vs its label (FR-2..FR-11, FR-16 signals).

Each dataset group runs with a fresh router store and reset mocks (D-026), emails in received order.
"""

from __future__ import annotations

from router.core.clock import FixedClock
from router.dataset.loader import load_records, reference_now
from router.decide.signals import ToneSignals, compute_signals
from router.pipeline.runner import Router
from router.pipeline.understanding import Understanding
from router.store.db import Store


def label_oracle(records):
    """Understanding taken from the labels: tests identity without any LLM."""
    by_mid: dict[str, Understanding] = {}
    for lr in records:
        l = lr.record.label
        hints = tuple(h for i in l.intents for h in i.entities.items)
        by_mid.setdefault(lr.email.message_id, Understanding(tuple(i.intent for i in l.intents), hints, l.language))
    return lambda email: by_mid[email.message_id]


def test_intake_and_identity_match_labels(pinned_config, mock_clients, mocks_reset) -> None:
    cfg = pinned_config.settings
    now = reference_now(cfg.paths.dataset)
    records = load_records(cfg.paths.dataset)
    groups: dict[str, list] = {}
    for lr in sorted(records, key=lambda r: (r.email.received_at, r.record.id)):
        groups.setdefault(lr.record.group_id, []).append(lr)

    oracle = label_oracle(records)
    mismatches: list[str] = []
    checked = 0
    for group in groups.values():
        mocks_reset(mock_clients)
        router = Router(pinned_config, Store(":memory:"), FixedClock(now), understander=oracle,
                        http_clients=mock_clients)
        case_of: dict[str, str] = {}
        for lr in group:
            ctx = router.process_email(lr.email)
            label, rid = lr.record.label, lr.record.id
            case_of[rid] = ctx.case_id
            disp = label.case.disposition

            if (disp == "duplicate_ignored") != (ctx.stage == "duplicate_ignored"):
                mismatches.append(f"{rid}: duplicate label {disp}, stage {ctx.stage}")
            if disp == "merged" and not ctx.intake.near_duplicate_of:
                mismatches.append(f"{rid}: labelled merged but no near-duplicate candidate")
            if disp != "merged" and ctx.intake.near_duplicate_of:
                mismatches.append(f"{rid}: unexpected near-duplicate candidate")
            if ctx.stage == "closed_automated" and label.intent_names != {"spam_or_auto"}:
                mismatches.append(f"{rid}: real email closed as automated (FR-6 precision)")
            if ctx.identity is None or disp in {"duplicate_ignored", "merged"}:
                continue
            checked += 1
            ident = ctx.identity
            if label.ownership != "not_applicable" and ident.ownership != label.ownership:
                mismatches.append(f"{rid}: ownership {ident.ownership}, label {label.ownership}")
            res = ident.resolution
            if label.order.startswith("ORD-") and label.order not in res.order_ids:
                mismatches.append(f"{rid}: order {label.order} not resolved ({res.status} {res.order_ids})")
            if label.order in {"ambiguous", "none", "not_needed"} and res.status != label.order:
                mismatches.append(f"{rid}: resolution {res.status}, label {label.order}")
            verified = ident.is_verified
            orders = [f.order for f in ident.verified.values()]
            signals = compute_signals(ToneSignals(), ident.customer, verified, orders, ident.contact_count, cfg)
            for sig in ("vip", "high_value", "repeat_contact"):
                if (sig in signals) != (sig in label.escalation):
                    mismatches.append(f"{rid}: signal {sig} computed={sig in signals} label={sig in label.escalation}")
    assert checked > 250
    assert not mismatches, "\n".join(mismatches)
