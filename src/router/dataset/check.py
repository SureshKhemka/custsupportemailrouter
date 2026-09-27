"""DS-5: verify labels are consistent with seed data, policy and routing configuration.

Labels are written by hand. This check recomputes everything that code decides (ownership,
delivery state, eligibility, remedies, amounts, computable escalation signals, case mode,
disposition, queue, actions) from the seed data and the active config, and reports every
disagreement. It also checks dataset-level requirements (DS-1..DS-3).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mocks.seed import load_seed_files
from router.config.models import Settings
from router.dataset.loader import LoadedRecord, load_gate_cases, load_rated_replies, reference_now
from router.dataset.schema import ExpectedIntent
from router.decide import policy as P
from router.decide.routing import AUTO_ACTIONS, ORDER_BOUND, CaseInput, IntentInput, decide_case
from router.schemas.backend import Charge, Customer, Order, Product, Refund

# DS-3 coverage: every tag must appear on at least one record.
REQUIRED_TAGS = {
    "multi_intent", "late_plus_double_charge", "return_one_where_other",
    "no_order_id_single_match", "no_order_id_multiple_matches",
    "not_owner", "unknown_sender", "secondary_email",
    "return_window_inside", "return_window_boundary", "return_window_outside", "non_returnable",
    "damaged_with_photo", "damaged_without_photo", "damage_reported_late",
    "legal_explicit", "legal_implicit", "legal_in_routine",
    "abuse", "angry_legitimate", "prompt_injection", "wrong_facts_quoted",
    "duplicate_submission", "follow_up", "third_contact",
    "out_of_office", "bounce", "spam", "very_short", "very_long", "code_mixed", "unsupported_language",
}
REQUIRED_LANGUAGES = {"en", "hi", "es", "de"}
# DS-6: defects the bad-reply set must cover.
REQUIRED_DEFECTS = {"wrong_amount", "wrong_date", "other_customer_name", "claimed_refund_not_happened",
                    "promise_outside_policy", "wrong_language", "leftover_placeholder"}
MIN_RATED_REPLIES = 30  # DS-7
# "ORD-100045", "ORD 100045", "#100045", "order 100045", "order no. 100045"
ORDER_ID_IN_TEXT = re.compile(r"(?:\bORD[-\s]?|#|\border\s+(?:no\.?\s*|number\s*)?)(\d{6})\b", re.IGNORECASE)


@dataclass
class SeedView:
    customers: list[Customer]
    orders: dict[str, Order]
    products: dict[str, Product]
    refunds: dict[str, list[Refund]]
    charges: dict[str, list[Charge]]

    @classmethod
    def load(cls, seed_dir: Path, now: datetime) -> SeedView:
        data, _ = load_seed_files(seed_dir, ("customers.json", "orders.json", "catalog.json", "refunds.json",
                                             "payments.json"), now)
        refunds: dict[str, list[Refund]] = defaultdict(list)
        for r in data["refunds"]["refunds"]:
            refunds[r["order_id"]].append(Refund.model_validate(r))
        charges: dict[str, list[Charge]] = defaultdict(list)
        for c in data["payments"]["charges"]:
            charges[c["order_id"]].append(Charge.model_validate(c))
        return cls(
            customers=[Customer.model_validate(c) for c in data["customers"]["customers"]],
            orders={o["order_id"]: Order.model_validate(o) for o in data["orders"]["orders"]},
            products={p["sku"]: Product.model_validate(p) for p in data["catalog"]["products"]},
            refunds=refunds, charges=charges,
        )

    def customer_by_email(self, email: str) -> Customer | None:
        e = email.strip().lower()
        return next((c for c in self.customers if e in (x.lower() for x in c.emails)), None)

    def recent_orders(self, customer_id: str, now: datetime, days: int) -> list[Order]:
        cutoff = now - timedelta(days=days)
        return [o for o in self.orders.values() if o.customer_id == customer_id and o.placed_at >= cutoff]


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


class _Ctx:
    def __init__(self, records: list[LoadedRecord], seed: SeedView, cfg: Settings, now: datetime):
        self.records, self.seed, self.cfg, self.now = records, seed, cfg, now
        self.tz = ZoneInfo(cfg.app.timezone)
        self.groups: dict[str, list[LoadedRecord]] = defaultdict(list)
        for lr in sorted(records, key=lambda r: (r.email.received_at, r.record.id)):
            self.groups[lr.record.group_id].append(lr)


def check_dataset(records: list[LoadedRecord], cfg: Settings, dataset_dir: Path) -> CheckResult:
    now = reference_now(dataset_dir)
    ctx = _Ctx(records, SeedView.load(cfg.paths.seed, now), cfg, now)
    res = CheckResult()
    for lr in records:
        for p in _check_record(lr, ctx):
            res.problems.append(f"{lr.record.id} ({lr.source.parent.name}/{lr.source.name}): {p}")
    res.problems += _check_dataset_level(records, ctx, res.stats)
    res.problems += _check_reply_sets(records, ctx, dataset_dir, res.stats)
    return res


# --------------------------------------------------------------------------- per record


def _check_record(lr: LoadedRecord, ctx: _Ctx) -> list[str]:
    rec, email, label = lr.record, lr.email, lr.record.label
    cfg, seed = ctx.cfg, ctx.seed
    probs: list[str] = []
    received = email.received_at

    pairs = [(i.intent, i.order_id) for i in label.intents]
    if len(pairs) != len(set(pairs)):
        probs.append("same intent listed twice for the same order")

    # ---- identity (FR-7, FR-9)
    customer = seed.customer_by_email(email.sender)
    order = seed.orders.get(label.order) if label.order.startswith("ORD-") else None
    if label.order.startswith("ORD-") and order is None:
        probs.append(f"order {label.order} not in seed")
    for it in label.intents:
        if it.order_id and it.order_id not in seed.orders:
            probs.append(f"{it.intent}: order {it.order_id} not in seed")
    order_bound = bool(label.intent_names & ORDER_BOUND)
    if customer is None:
        ownership = "unknown_sender"
    elif order is not None and order.customer_id != customer.customer_id:
        ownership = "not_owner"
    else:
        ownership = "owner"
    expected_own = label.ownership if label.ownership != "not_applicable" else ownership
    if label.ownership == "not_applicable" and order_bound:
        probs.append("ownership 'not_applicable' but the email has an order-bound intent")
    if expected_own != ownership:
        probs.append(f"ownership: label {label.ownership}, seed says {ownership}")
    verified = ownership == "owner"

    # ---- order resolution (FR-8, FR-11)
    resolution = {"ambiguous": "ambiguous", "none": "none", "not_needed": "not_needed"}.get(label.order, "resolved")
    if customer and label.order in {"ambiguous", "none"}:
        recent = seed.recent_orders(customer.customer_id, received, cfg.handling.ambiguous_order.candidate_window_days)
        if label.order == "ambiguous" and len(recent) < 2:
            probs.append(f"order 'ambiguous' but customer has {len(recent)} recent order(s)")
        if label.order == "none" and recent:
            probs.append(f"order 'none' but customer has recent orders {[o.order_id for o in recent]}")
    if order_bound and label.order == "not_needed":
        probs.append("order-bound intent but order is 'not_needed'")
    if verified and customer and label.order.startswith("ORD-") and not ORDER_ID_IN_TEXT.search(
            f"{email.subject} {email.body}"):
        recent = seed.recent_orders(customer.customer_id, received, cfg.handling.ambiguous_order.candidate_window_days)
        if [o.order_id for o in recent] != [label.order] and "wrong_facts_quoted" not in rec.tags \
                and not rec.group:
            probs.append(f"no order id in text, but {label.order} is not the customer's only recent order")

    # ---- duplicates / merges (FR-2, FR-3)
    group = ctx.groups[rec.group_id]
    earlier = [g for g in group if (g.email.received_at, g.record.id) < (received, rec.id)]
    duplicate = any(g.email.message_id == email.message_id for g in earlier)
    merged = label.case.disposition == "merged"
    if merged:
        window = timedelta(minutes=cfg.intake.near_duplicate_window_minutes)
        if not any(g.email.sender == email.sender and received - g.email.received_at <= window for g in earlier):
            probs.append("disposition 'merged' but no earlier email from the same sender within the window")

    # A duplicate or merged email is not processed on its own: nothing is decided for it.
    processed = not (duplicate or merged)

    # ---- per-intent decisions and modes
    intent_inputs = []
    computed_amounts: set[float] = set()
    for it in label.intents:
        conds, dprobs, amounts = _intent_decision(it, ctx, email,
                                                  processed and verified and resolution == "resolved")
        probs += [f"{it.intent}: {p}" for p in dprobs]
        computed_amounts |= amounts
        intent_inputs.append(IntentInput(it.intent, frozenset(conds)))

    # ---- computable escalation signals (FR-16)
    signals = set(label.escalation)
    computed = set()
    if customer and verified and customer.tier in cfg.escalation.vip_tiers:
        computed.add("vip")
    ref_orders = [seed.orders[i.order_id] for i in label.intents if i.order_id in seed.orders]
    if verified and any(o.total > cfg.escalation.high_order_value.threshold for o in ref_orders):
        computed.add("high_value")
    if verified and customer and _contact_count(lr, ctx, customer) >= cfg.escalation.repeat_contact.min_contacts:
        computed.add("repeat_contact")
    for sig in ("vip", "high_value", "repeat_contact"):
        if processed and (sig in signals) != (sig in computed):
            probs.append(f"escalation '{sig}': label {'has' if sig in signals else 'lacks'} it, "
                         f"seed says {'yes' if sig in computed else 'no'}")

    # ---- case decision (FR-15..FR-20)
    lang_ok = label.language in cfg.app.supported_languages
    handling = decide_case(CaseInput(
        intents=tuple(intent_inputs), language_supported=lang_ok, ownership=ownership,  # type: ignore[arg-type]
        order_resolution=resolution, signals=frozenset(signals), injection=label.injection,  # type: ignore[arg-type]
        duplicate=duplicate, merged=merged), cfg)
    for fld in ("mode", "disposition", "queue", "priority"):
        want, got = getattr(label.case, fld), getattr(handling, fld)
        if want != got:
            probs.append(f"case.{fld}: label {want!r}, policy/config gives {got!r}")
    for it in label.intents:
        got = handling.intent_modes.get(it.intent)
        if got and it.mode != got:
            probs.append(f"{it.intent}: mode label {it.mode}, config gives {got}")

    # ---- actions (FR-20, FR-24)
    want_run = {_action_key(a) for a in _auto_actions(handling.run_actions_for, label.intents, ctx)}
    got_run = {_action_key(a.model_dump()) for a in label.actions}
    if want_run != got_run:
        probs.append(f"actions: label {sorted(got_run)}, expected {sorted(want_run)}")
    held = {_action_key(a) for a in _auto_actions(handling.hold_actions_for, label.intents, ctx)}
    proposed = {_action_key(a.model_dump()) for a in label.proposed_actions}
    if not held <= proposed:
        probs.append(f"proposed_actions missing held actions {sorted(held - proposed)}")
    if set(label.forbidden_actions) & {a.type for a in label.actions}:
        probs.append("an action is both expected and forbidden")
    if not verified and (label.actions or label.proposed_actions):
        probs.append("actions for an unverified sender")

    # ---- reply facts (DS-4)
    no_reply = handling.disposition in {"closed_spam", "duplicate_ignored", "merged"} or \
        handling.mode == "ROUTE_NO_DRAFT"
    if no_reply and label.reply.must_contain:
        probs.append("reply.must_contain set but this case gets no reply or draft")
    if handling.disposition == "identity_reply":
        leaked = [f for f in label.reply.must_contain if f.kind in {"order_id", "tracking", "amount", "date"}]
        if leaked:
            probs.append(f"identity reply must not contain order facts: {leaked}")
    for f in label.reply.must_contain:
        if f.kind == "order_id" and f.value not in {i.order_id for i in label.intents}:
            probs.append(f"reply order_id {f.value} is not a labelled intent order")
        if f.kind == "tracking" and f.value not in {o.tracking_number for o in ref_orders}:
            probs.append(f"reply tracking {f.value} does not belong to the referenced order(s)")
        if f.kind == "amount" and float(f.value) not in computed_amounts:
            probs.append(f"reply amount {f.value} is not one computed from seed/policy {sorted(computed_amounts)}")
    return probs


def _intent_decision(it: ExpectedIntent, ctx: _Ctx, email, can_decide: bool) -> tuple[set[str], list[str], set[float]]:
    """Recompute the policy decision for one intent; return (conditions, problems, amounts)."""
    d, probs, amounts, conds = it.decision, [], set(), set()
    order = ctx.seed.orders.get(it.order_id or "")
    if order is None or not can_decide:
        if d:
            probs.append("decision given but the system does not decide here "
                         "(unverified sender, no single order, or duplicate/merged email)")
        return conds, probs, amounts
    cfg, now, tz = ctx.cfg, email.received_at, ctx.tz
    lines = [P.LineRequest(l["line_id"], l["qty"]) for l in d.get("lines", [])] or P.whole_lines(order)

    def expect(key: str, value: Any) -> None:
        if key not in d:
            probs.append(f"decision.{key} missing (expected {value!r})")
        elif d[key] != value:
            probs.append(f"decision.{key}: label {d[key]!r}, policy gives {value!r}")

    if it.intent == "order_status":
        state = P.delivery_state(order, now, cfg.policy, tz)
        expect("delivery_state", state)
        if state == "lost":
            conds.add("lost_shipment")
    elif it.intent == "return_request":
        rd = P.return_eligibility(order, lines, now, cfg.policy, tz)
        expect("eligible", rd.eligible)
        expect("reason", rd.reason)
        if not rd.eligible:
            conds.add("not_eligible")
    elif it.intent in {"damaged_item", "wrong_item"}:
        stock = {sku: p.in_stock for sku, p in ctx.seed.products.items()}
        kind = "damaged" if it.intent == "damaged_item" else "wrong_item"
        dd = P.damage_remedy(order, lines, kind, now, stock, email.has_photos, cfg.policy, tz)  # type: ignore[arg-type]
        expect("remedy", dd.remedy)
        expect("reason", dd.reason)
        if dd.refund_amount is not None:
            expect("refund_amount", dd.refund_amount)
            amounts.add(dd.refund_amount)
    elif it.intent == "cancel_order":
        ok = P.can_cancel(order, cfg.policy)
        expect("cancellable", ok)
        if not ok:
            conds.add("already_shipped")
    elif it.intent == "refund_status":
        rs = P.refund_status(ctx.seed.refunds.get(order.order_id, []))
        expect("state", rs.state)
        expect("amount", rs.amount)
        amounts.add(rs.amount)
    elif it.intent in {"billing_dispute", "payment_issue"}:
        charges = ctx.seed.charges.get(order.order_id, [])
        dup = sum(c.status == "succeeded" for c in charges) >= 2
        failed = any(c.status == "failed" for c in charges)
        if "duplicate_charge" in d:
            expect("duplicate_charge", dup)
        if "failed_payment" in d:
            expect("failed_payment", failed)
        amounts |= {c.amount for c in charges}
    amounts |= {order.total, order.subtotal} | {l.unit_price for l in order.items}
    return conds, probs, amounts


def _contact_count(lr: LoadedRecord, ctx: _Ctx, customer: Customer) -> int:
    """This contact plus earlier contacts about the same order within the repeat-contact window."""
    rc = ctx.cfg.escalation.repeat_contact
    order_id = lr.record.label.order
    received = lr.email.received_at
    window_start = received - timedelta(days=rc.window_days)
    count = 1
    if order_id.startswith("ORD-"):
        count += sum(1 for h in customer.contact_history if h.order_id == order_id and window_start <= h.at < received)
    seen_ids = {lr.email.message_id}
    for g in ctx.groups[lr.record.group_id]:
        if (g.email.received_at, g.record.id) >= (received, lr.record.id):
            break
        if g.email.message_id in seen_ids or g.record.label.case.disposition in {"merged", "closed_spam"}:
            continue
        if g.email.received_at >= window_start and g.record.label.order == order_id:
            seen_ids.add(g.email.message_id)
            count += 1
    return count


def _auto_actions(names: tuple[str, ...], intents: list[ExpectedIntent], ctx: _Ctx) -> list[dict]:
    out = []
    for it in intents:
        if it.intent not in names or not it.order_id:
            continue
        action = {"type": AUTO_ACTIONS[it.intent], "order_id": it.order_id, "lines": []}
        if it.intent == "return_request":
            lines = it.decision.get("lines") or [
                {"line_id": l.line_id, "qty": l.qty} for l in P.whole_lines(ctx.seed.orders[it.order_id])]
            action["lines"] = lines
        out.append(action)
    return out


def _action_key(a: dict) -> tuple:
    return (a["type"], a["order_id"], tuple(sorted((l["line_id"], l["qty"]) for l in a.get("lines", []))))


# --------------------------------------------------------------------------- dataset level


def _check_dataset_level(records: list[LoadedRecord], ctx: _Ctx, stats: dict[str, Any]) -> list[str]:
    probs = []
    ids = Counter(lr.record.id for lr in records)
    probs += [f"duplicate record id {i}" for i, n in ids.items() if n > 1]
    split_of_group: dict[str, set[str]] = defaultdict(set)
    for lr in records:
        split_of_group[lr.record.group_id].add(lr.split)
    probs += [f"group {g} spans splits {sorted(s)}" for g, s in split_of_group.items() if len(s) > 1]
    msg_ids = Counter(lr.email.message_id for lr in records)
    for mid, n in msg_ids.items():
        if n > 1 and not all("duplicate_submission" in lr.record.tags for lr in records
                             if lr.email.message_id == mid and lr.record.label.case.disposition == "duplicate_ignored"):
            probs.append(f"message id {mid} used {n} times without a duplicate_submission label")

    total = len(records)
    by_split = Counter(lr.split for lr in records)
    langs = Counter(lr.record.label.language for lr in records)
    non_en = sum(n for lang, n in langs.items() if lang != "en")
    multi = sum(len(lr.record.label.intents) > 1 for lr in records)
    tags = Counter(t for lr in records for t in lr.record.tags)
    intents = Counter(i.intent for lr in records for i in lr.record.label.intents)
    single = Counter(lr.record.label.intents[0].intent for lr in records if len(lr.record.label.intents) == 1)
    dispositions = Counter(lr.record.label.case.disposition for lr in records)
    stats.update(total=total, by_split=dict(by_split), languages=dict(langs),
                 non_english_share=round(non_en / total, 3) if total else 0,
                 multi_intent_share=round(multi / total, 3) if total else 0,
                 intents=dict(intents), dispositions=dict(dispositions), tags=dict(tags))

    if total < 200:
        probs.append(f"DS-1: {total} records, need at least 200")
    if not by_split.get("test"):
        probs.append("DS-1: no held-out test records")
    if total and non_en / total < 0.10:
        probs.append(f"DS-2: non-English share {non_en / total:.1%} < 10%")
    missing_langs = REQUIRED_LANGUAGES - set(langs)
    if missing_langs:
        probs.append(f"DS-2: missing languages {sorted(missing_langs)}")
    if not any(l not in set(ctx.cfg.app.supported_languages) | REQUIRED_LANGUAGES for l in langs):
        probs.append("DS-2: no email in a language outside the supported and required set (e.g. French)")
    if total and multi / total < 0.15:
        probs.append(f"DS-3: multi-intent share {multi / total:.1%} < 15%")
    missing_tags = REQUIRED_TAGS - set(tags)
    if missing_tags:
        probs.append(f"DS-3: missing edge-case tags {sorted(missing_tags)}")
    missing_simple = set(ctx.cfg.taxonomy.intents) - set(single)
    if missing_simple:
        probs.append(f"DS-3: no single-intent example for {sorted(missing_simple)}")
    return probs


def _check_reply_sets(records: list[LoadedRecord], ctx: _Ctx, dataset_dir: Path, stats: dict[str, Any]) -> list[str]:
    """DS-6 and DS-7 files: valid, reference dev records only, and cover what they must."""
    probs: list[str] = []
    dev_ids = {lr.record.id for lr in records if lr.split == "dev"}
    all_ids = {lr.record.id for lr in records}
    try:
        gate_cases = load_gate_cases(dataset_dir, ctx.tz)
        rated = load_rated_replies(dataset_dir)
    except Exception as exc:  # schema errors are reported, not raised
        return [f"DS-6/DS-7 files invalid: {exc}"]
    if not all_ids:  # a partial load (e.g. --split test) cannot resolve references
        return probs
    for case in gate_cases:
        if case.record not in dev_ids:
            probs.append(f"DS-6 {case.id}: record {case.record} is not a dev record")
    for r in rated:
        if r.record not in dev_ids:
            probs.append(f"DS-7 {r.id}: record {r.record} is not a dev record")
        rubrics = set(ctx.cfg.evals.judge.rubrics)
        if set(r.ratings) != rubrics:
            probs.append(f"DS-7 {r.id}: ratings must cover exactly {sorted(rubrics)}")
    defects = {c.defect for c in gate_cases if c.expect == "block"}
    missing = REQUIRED_DEFECTS - defects
    if missing:
        probs.append(f"DS-6: missing bad-reply defects {sorted(missing)}")
    if not any(c.expect == "pass" for c in gate_cases):
        probs.append("DS-6: no 'pass' controls (needed to catch an over-blocking gate)")
    if len(rated) < MIN_RATED_REPLIES:
        probs.append(f"DS-7: {len(rated)} rated replies, need at least {MIN_RATED_REPLIES}")
    stats.update(gate_cases_block=sum(c.expect == "block" for c in gate_cases),
                 gate_cases_pass=sum(c.expect == "pass" for c in gate_cases),
                 rated_replies=len(rated), ratings_pending_human_review=sum(r.is_draft for r in rated))
    return probs
