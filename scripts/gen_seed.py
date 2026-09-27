"""Generate deterministic seed data for the mock services (SD-1..SD-6).

Run:  uv run python scripts/gen_seed.py
Writes seed/*.json. Output is fully deterministic; re-running produces identical files.

Every date is written relative to the injectable "now" as "@now-29d", "@now+2.5d" or "@now",
so the data never goes stale (SD-6). The mocks resolve these when they load or reset.

Named scenarios (seed/scenarios.json) are hand-picked records the labelled dataset refers
to. Everything else is filler that widens coverage.

Notes on order status: the order service reports the raw carrier/fulfilment status.
"Delayed" (past promised date) and "lost" (no tracking update for N days) are derived by
router code from dates (PO-4), so those orders are `in_transit` with the right dates.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "seed"
RNG = random.Random(20260927)

# --------------------------------------------------------------------------- catalogue

# sku, name, category, price (INR), in_stock
CATALOG: list[tuple[str, str, str, int, bool]] = [
    ("SKU-EL-001", "Wireless Earbuds Pro", "electronics", 2499, True),
    ("SKU-EL-002", "Noise Cancelling Headphones", "electronics", 7999, True),
    ("SKU-EL-003", "Smartphone X12 128GB", "electronics", 24999, True),
    ("SKU-EL-004", "Ultrabook Laptop 14", "electronics", 64999, True),
    ("SKU-EL-005", "Smartwatch Fit 3", "electronics", 8999, False),
    ("SKU-EL-006", "65W USB-C Charger", "electronics", 1299, True),
    ("SKU-EL-007", "Power Bank 20000mAh", "electronics", 1799, True),
    ("SKU-EL-008", "4K Smart TV 55in", "electronics", 52999, True),
    ("SKU-EL-009", "Bluetooth Speaker Mini", "electronics", 1999, False),
    ("SKU-AP-001", "Cotton Crew T-Shirt", "apparel", 699, True),
    ("SKU-AP-002", "Slim Fit Jeans", "apparel", 1999, True),
    ("SKU-AP-003", "Running Shoes Air", "apparel", 3499, True),
    ("SKU-AP-004", "Winter Puffer Jacket", "apparel", 4599, False),
    ("SKU-AP-005", "Linen Kurta", "apparel", 1499, True),
    ("SKU-HM-001", "Mixer Grinder 750W", "home", 3999, True),
    ("SKU-HM-002", "Cotton Bedsheet Set", "home", 1299, True),
    ("SKU-HM-003", "Non-stick Cookware Set", "home", 5499, False),
    ("SKU-HM-004", "LED Desk Lamp", "home", 1799, True),
    ("SKU-HM-005", "Ceramic Dinner Set", "home", 2999, True),
    ("SKU-HM-006", "Air Fryer 4L", "home", 6999, True),
    ("SKU-BK-001", "The Pragmatic Cook (Book)", "books", 499, True),
    ("SKU-BK-002", "Mystery of the Hills (Book)", "books", 399, True),
    ("SKU-BT-001", "Vitamin C Face Serum", "beauty", 899, True),
    ("SKU-BT-002", "Hair Dryer Compact", "beauty", 1599, True),
    ("SKU-PR-001", "Assorted Chocolate Box", "perishable", 899, True),
    ("SKU-PR-002", "Premium Dry Fruits 1kg", "perishable", 1499, True),
    ("SKU-PR-003", "Fresh Alphonso Mangoes 2kg", "perishable", 1199, False),
    ("SKU-PS-001", "Engraved Photo Mug", "personalised", 699, True),
    ("SKU-PS-002", "Custom Name T-Shirt", "personalised", 999, True),
    ("SKU-PS-003", "Engraved Steel Bottle", "personalised", 1299, True),
    ("SKU-HY-001", "Electric Toothbrush", "hygiene", 2299, True),
    ("SKU-HY-002", "Trimmer Pro", "hygiene", 1899, True),
    ("SKU-HY-003", "Innerwear Pack of 3", "hygiene", 599, True),
]
PRODUCTS = {sku: {"sku": sku, "name": n, "category": c, "price": p, "in_stock": s} for sku, n, c, p, s in CATALOG}
FILLER_SKUS = [s for s, *_ in CATALOG if s not in {"SKU-EL-004", "SKU-EL-008"}]  # keep high-value orders deliberate

CARRIERS = {"BlueDart": "BD", "Delhivery": "DL", "Ekart": "EK", "DTDC": "DT"}
CITIES = [("Mumbai", "400001"), ("Delhi", "110001"), ("Bengaluru", "560001"), ("Chennai", "600001"),
          ("Hyderabad", "500001"), ("Pune", "411001"), ("Kolkata", "700001"), ("Jaipur", "302001"),
          ("Ahmedabad", "380001"), ("Kochi", "682001")]
HUBS = ["Bhiwandi Hub", "Gurgaon Hub", "Hoskote Hub", "Nagpur Hub", "Hyderabad Hub"]

# --------------------------------------------------------------------------- customers

NAMES = [
    "Aarav Sharma", "Priya Patel", "Rohan Mehta", "Ananya Iyer", "Vikram Singh", "Sneha Reddy",
    "Arjun Nair", "Kavya Menon", "Rahul Gupta", "Isha Kapoor", "Aditya Rao", "Meera Joshi",
    "Karan Malhotra", "Divya Pillai", "Siddharth Bose", "Pooja Desai", "Nikhil Verma", "Riya Chatterjee",
    "Manish Agarwal", "Neha Kulkarni", "Suresh Krishnan", "Lakshmi Subramanian", "Harsh Vardhan",
    "Tanvi Shah", "Amit Chauhan", "Shruti Bhatt", "Varun Khanna", "Aishwarya Das", "Deepak Yadav",
    "Nandini Hegde", "Farhan Qureshi", "Zoya Khan", "Gurpreet Kaur", "Rajesh Pandey", "Swati Mishra",
    "Kunal Saxena", "Anjali Tiwari", "Mohit Jain", "Ritika Arora", "Sanjay Dutta", "Emily Carter",
    "James Wilson", "Sofia Garcia", "Lukas Schneider", "Chen Wei", "Olivia Brown", "Daniel Fischer",
    "Maria Lopez", "Tom Becker", "Hannah Weber", "Abhishek Rane", "Payal Sethi", "Yash Thakur",
    "Bhavna Goel", "Irfan Sheikh", "Rekha Nambiar", "Sameer Kohli", "Alisha Fernandes", "Gaurav Bhatia",
    "Mehul Doshi", "Sana Mirza", "Pranav Kulkarni", "Jyoti Rawat", "Ramesh Iyengar", "Carlos Ruiz",
]


def email_of(name: str, domain: str = "example.com") -> str:
    first, last = name.lower().split()
    return f"{first}.{last}@{domain}"


def rel(days: float) -> str:
    """Relative datetime: negative = in the past. rel(-29) -> '@now-29d'."""
    if days == 0:
        return "@now"
    d = round(days, 3)
    txt = f"{d:+.3f}".rstrip("0").rstrip(".")
    return f"@now{txt}d"


@dataclass
class Seed:
    customers: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    charges: list[dict] = field(default_factory=list)
    payment_events: list[dict] = field(default_factory=list)
    returns: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)
    scenarios: dict[str, dict] = field(default_factory=dict)
    _order_seq: int = 100000
    _charge_seq: int = 500000
    _event_seq: int = 700000

    # ------------------------------------------------------------------ ids

    def next_order_id(self) -> str:
        self._order_seq += 1
        return f"ORD-{self._order_seq}"

    def next_charge_id(self) -> str:
        self._charge_seq += 1
        return f"CH-{self._charge_seq}"

    def next_event_id(self) -> str:
        self._event_seq += 1
        return f"PE-{self._event_seq}"

    def customer(self, cid: str) -> dict:
        return next(c for c in self.customers if c["customer_id"] == cid)

    def scenario(self, name: str, description: str, **refs: Any) -> None:
        assert name not in self.scenarios, name
        self.scenarios[name] = {"description": description, **refs}

    # ------------------------------------------------------------------ orders

    def order(
        self,
        customer_id: str,
        items: list[tuple[str, int]],
        status: str,
        *,
        placed_days_ago: float | None = None,
        delivered_days_ago: float | None = None,
        promised_in_days: float | None = None,
        last_update_days_ago: float | None = None,
        payment: str | None = None,
        payment_outcome: str = "paid",
        tags: tuple[str, ...] = (),
    ) -> dict:
        """Build a consistent order for the given status. Day arguments are relative to now."""
        oid = self.next_order_id()
        cust = self.customer(customer_id)
        lines = []
        for i, (sku, qty) in enumerate(items, start=1):
            p = PRODUCTS[sku]
            lines.append({"line_id": f"{oid}-L{i}", "sku": sku, "name": p["name"], "category": p["category"],
                          "qty": qty, "unit_price": p["price"], "returned_qty": 0})
        subtotal = sum(l["qty"] * l["unit_price"] for l in lines)
        shipping = 0 if subtotal >= 1000 else 79
        carrier = RNG.choice(list(CARRIERS))
        payment = payment or RNG.choice(["card", "card", "upi", "upi", "netbanking", "cod"])
        if status in {"cancelled", "return_in_progress", "returned", "refunded"} and payment == "cod":
            payment = "upi"

        o: dict[str, Any] = {
            "order_id": oid, "customer_id": customer_id, "status": status, "items": lines,
            "subtotal": subtotal, "shipping_fee": shipping, "total": subtotal + shipping, "currency": "INR",
            "payment_method": _payment_method(payment),
            "shipping_address": {"name": cust["name"], **cust["address"]},
            "placed_at": None, "shipped_at": None, "promised_delivery_date": None, "delivered_at": None,
            "cancelled_at": None, "carrier": None, "tracking_number": None, "tracking_events": [],
        }

        # --- timeline per status
        if status in {"placed", "packed"}:
            placed = placed_days_ago if placed_days_ago is not None else (0.3 if status == "placed" else 1.0)
            o["placed_at"] = rel(-placed)
            o["promised_delivery_date"] = rel(promised_in_days if promised_in_days is not None else 4)
        elif status == "cancelled":
            placed = placed_days_ago if placed_days_ago is not None else RNG.randint(3, 40)
            o["placed_at"] = rel(-placed)
            o["cancelled_at"] = rel(-(placed - 0.2))
            o["promised_delivery_date"] = rel(-(placed - 5))
        elif status in {"shipped", "in_transit", "out_for_delivery"}:
            shipped = (placed_days_ago - 1) if placed_days_ago is not None else {"shipped": 0.5, "in_transit": 2.0,
                                                                                  "out_for_delivery": 3.0}[status]
            placed = shipped + 1
            promised = promised_in_days if promised_in_days is not None else (0 if status == "out_for_delivery" else 2)
            last = last_update_days_ago if last_update_days_ago is not None else max(0.2, min(shipped, 0.5))
            o["placed_at"], o["shipped_at"] = rel(-placed), rel(-shipped)
            o["promised_delivery_date"] = rel(promised)
            o["carrier"], o["tracking_number"] = carrier, _tracking(carrier)
            o["tracking_events"] = _transit_events(shipped, last, cust["address"]["city"],
                                                   out_for_delivery=(status == "out_for_delivery"))
        else:  # delivered and post-delivery statuses
            delivered = delivered_days_ago if delivered_days_ago is not None else RNG.randint(1, 80)
            shipped = delivered + RNG.choice([2, 3, 3, 4])
            placed = shipped + 1
            promised_offset = -(delivered + RNG.choice([0, 0, 1, 2]))  # on time or a little early
            o["placed_at"], o["shipped_at"], o["delivered_at"] = rel(-placed), rel(-shipped), rel(-delivered)
            o["promised_delivery_date"] = rel(promised_offset if promised_in_days is None else promised_in_days)
            o["carrier"], o["tracking_number"] = carrier, _tracking(carrier)
            o["tracking_events"] = _delivered_events(shipped, delivered, cust["address"]["city"])

        if tags:
            o["_tags"] = list(tags)  # informational only; stripped from API responses
        self.orders.append(o)
        self._payments_for(o, payment_outcome)
        return o

    def _payments_for(self, o: dict, outcome: str) -> None:
        """outcome: paid | failed (unpaid) | failed_then_paid (declined, customer retried)."""
        if o["payment_method"]["type"] == "cod":
            if o["delivered_at"]:
                self.charge(o, o["total"], "succeeded", at=o["delivered_at"], note="Cash collected on delivery")
            return
        if outcome in {"failed", "failed_then_paid"}:
            self.charge(o, o["total"], "failed", at=o["placed_at"])
        if outcome in {"paid", "failed_then_paid"}:
            at = rel(-(_days(o["placed_at"]) - 0.01)) if outcome == "failed_then_paid" else o["placed_at"]
            self.charge(o, o["total"], "succeeded", at=at,
                        note="Payment captured on retry" if outcome == "failed_then_paid" else None)

    def charge(self, o: dict, amount: int, status: str, *, at: str, note: str | None = None) -> dict:
        ch = {"charge_id": self.next_charge_id(), "order_id": o["order_id"], "amount": amount, "currency": "INR",
              "status": status, "created_at": at, "method": o["payment_method"], "failure_reason": None}
        if status == "failed":
            ch["failure_reason"] = "issuer_declined"
        self.charges.append(ch)
        etype = {"succeeded": "charge_succeeded", "failed": "charge_failed"}[status]
        self.payment_events.append({"event_id": self.next_event_id(), "order_id": o["order_id"], "type": etype,
                                    "charge_id": ch["charge_id"], "amount": amount, "at": at,
                                    "detail": note or ("Payment captured" if status == "succeeded"
                                                       else "Card issuer declined the payment")})
        return ch

    def refund(self, o: dict, amount: int, status: str, *, created_days_ago: float, reason: str,
               processed_days_ago: float | None = None) -> dict:
        rid = f"RF-{o['order_id'][4:]}-{sum(1 for r in self.refunds if r['order_id'] == o['order_id']) + 1}"
        r = {"refund_id": rid, "order_id": o["order_id"], "amount": amount, "currency": "INR", "status": status,
             "reason": reason, "method": "original_payment_method", "created_at": rel(-created_days_ago),
             "processed_at": rel(-processed_days_ago) if processed_days_ago is not None else None,
             "idempotency_key": f"seed:{rid}"}
        self.refunds.append(r)
        if status == "processed":
            self.payment_events.append({"event_id": self.next_event_id(), "order_id": o["order_id"],
                                        "type": "refund_processed", "charge_id": None, "amount": amount,
                                        "at": r["processed_at"], "detail": f"Refund {rid} to original method"})
        return r

    def return_(self, o: dict, lines: list[tuple[int, int]], status: str, *, created_days_ago: float,
                reason: str) -> dict:
        rid = f"RT-{o['order_id'][4:]}-{sum(1 for r in self.returns if r['order_id'] == o['order_id']) + 1}"
        items = []
        for idx, qty in lines:
            line = o["items"][idx]
            items.append({"line_id": line["line_id"], "sku": line["sku"], "qty": qty})
            if status in {"received", "refunded"}:
                line["returned_qty"] += qty
        r = {"return_id": rid, "order_id": o["order_id"], "items": items, "reason": reason, "status": status,
             "created_at": rel(-created_days_ago), "pickup_scheduled_for": rel(-(created_days_ago - 2)),
             "idempotency_key": f"seed:{rid}"}
        self.returns.append(r)
        return r


def _payment_method(kind: str) -> dict:
    if kind == "card":
        return {"type": "card", "brand": RNG.choice(["Visa", "Mastercard", "RuPay"]),
                "last4": f"{RNG.randint(0, 9999):04d}"}
    if kind == "upi":
        return {"type": "upi", "vpa_masked": f"{RNG.choice('abcdefghkmnprs')}****@ok{RNG.choice(['axis', 'hdfc', 'icici', 'sbi'])}"}
    if kind == "netbanking":
        return {"type": "netbanking", "bank": RNG.choice(["HDFC Bank", "ICICI Bank", "SBI", "Axis Bank"])}
    return {"type": "cod"}


def _tracking(carrier: str) -> str:
    return f"{CARRIERS[carrier]}{RNG.randint(10**9, 10**10 - 1)}IN"


def _transit_events(shipped: float, last: float, city: str, *, out_for_delivery: bool) -> list[dict]:
    events = [{"at": rel(-shipped), "status": "picked_up", "location": "Seller Warehouse, Bhiwandi",
               "description": "Shipment picked up"}]
    t = shipped - 1
    while t > last:
        events.append({"at": rel(-t), "status": "in_transit", "location": RNG.choice(HUBS),
                       "description": "Arrived at sorting hub"})
        t -= 1
    if last < shipped:
        events.append({"at": rel(-last), "status": "out_for_delivery" if out_for_delivery else "in_transit",
                       "location": f"{city} Delivery Centre" if out_for_delivery else RNG.choice(HUBS),
                       "description": "Out for delivery" if out_for_delivery else "In transit to destination city"})
    return events


def _delivered_events(shipped: float, delivered: float, city: str) -> list[dict]:
    events = _transit_events(shipped, delivered + 0.3, city, out_for_delivery=True)
    events.append({"at": rel(-delivered), "status": "delivered", "location": city,
                   "description": "Delivered to customer"})
    return events


# --------------------------------------------------------------------------- build


def build() -> Seed:
    s = Seed()

    # ---- customers (SD-1)
    vip_idx = {1, 30, 41, 43, 44, 51, 60}
    for i, name in enumerate(NAMES, start=1):
        city, pin = CITIES[i % len(CITIES)]
        s.customers.append({
            "customer_id": f"CUST-{i:04d}", "name": name, "emails": [email_of(name)],
            "phone": f"+91 9{RNG.randint(100000000, 999999999)}",
            "tier": "vip" if i in vip_idx else "standard",
            "created_at": rel(-RNG.randint(60, 1500)),
            "address": {"line1": f"{RNG.randint(1, 300)}, {RNG.choice(['MG Road', 'Park Street', 'Linking Road', 'Anna Salai', 'Residency Road'])}",
                        "city": city, "pincode": pin},
            "contact_history": [],
        })
    for i, extra in [(2, "priya.p.shop@example.in"), (5, "vikram.work@example.org"), (9, "rahulg@example.in"),
                     (21, "suresh.k.home@example.in"), (24, "tanvi.personal@example.org"),
                     (33, "gurpreet.k@example.in"), (43, "sofia.g@example.org"), (51, "abhi.rane@example.in")]:
        s.customer(f"CUST-{i:04d}")["emails"].append(extra)

    c = lambda n: f"CUST-{n:04d}"  # noqa: E731

    # ---- one order per status (SD-2)
    for status, cust in [("placed", 3), ("packed", 4), ("shipped", 6), ("in_transit", 8),
                         ("out_for_delivery", 10), ("delivered", 11)]:
        o = s.order(c(cust), [(RNG.choice(FILLER_SKUS), 1)], status)
        s.scenario(f"status_{status}", f"Single order in status {status}", customer_id=c(cust),
                   order_id=o["order_id"])

    o = s.order(c(12), [("SKU-AP-003", 1)], "in_transit", placed_days_ago=7, promised_in_days=-2,
                last_update_days_ago=1, tags=("delayed",))
    s.scenario("status_delayed", "In transit, 2 days past promised date, tracking still updating",
               customer_id=c(12), order_id=o["order_id"])
    o = s.order(c(13), [("SKU-HM-001", 1)], "in_transit", placed_days_ago=14, promised_in_days=-8,
                last_update_days_ago=9, tags=("lost",))
    s.scenario("status_lost", "In transit, no tracking update for 9 days (lost at 7)", customer_id=c(13),
               order_id=o["order_id"])
    o = s.order(c(15), [("SKU-HM-004", 1)], "in_transit", placed_days_ago=10, promised_in_days=-4,
                last_update_days_ago=7, tags=("lost_boundary",))
    s.scenario("lost_boundary_7d", "No tracking update for exactly 7 days (lost boundary)", customer_id=c(15),
               order_id=o["order_id"])
    o = s.order(c(16), [("SKU-HM-005", 1)], "in_transit", placed_days_ago=9, promised_in_days=-3,
                last_update_days_ago=6, tags=("delayed",))
    s.scenario("not_lost_6d", "Delayed; last tracking update 6 days ago (not yet lost)", customer_id=c(16),
               order_id=o["order_id"])

    o = s.order(c(17), [("SKU-AP-002", 1)], "cancelled", placed_days_ago=6)
    s.refund(o, o["total"], "processed", created_days_ago=5.8, processed_days_ago=3, reason="order_cancelled")
    s.scenario("status_cancelled", "Cancelled prepaid order, refund processed", customer_id=c(17),
               order_id=o["order_id"])

    o = s.order(c(18), [("SKU-AP-003", 1)], "return_in_progress", delivered_days_ago=10)
    s.return_(o, [(0, 1)], "authorised", created_days_ago=3, reason="size_issue")
    s.scenario("status_return_in_progress", "Return authorised, pickup pending", customer_id=c(18),
               order_id=o["order_id"])

    o = s.order(c(19), [("SKU-HM-006", 1)], "returned", delivered_days_ago=20)
    s.return_(o, [(0, 1)], "received", created_days_ago=12, reason="not_as_expected")
    s.refund(o, o["subtotal"], "initiated", created_days_ago=1, reason="return_received")
    s.scenario("status_returned", "Return received; refund initiated, not yet processed", customer_id=c(19),
               order_id=o["order_id"])

    o = s.order(c(20), [("SKU-EL-002", 1)], "refunded", delivered_days_ago=25)
    s.return_(o, [(0, 1)], "refunded", created_days_ago=18, reason="changed_mind")
    s.refund(o, o["subtotal"], "processed", created_days_ago=12, processed_days_ago=9, reason="return_received")
    s.scenario("status_refunded", "Returned and refunded", customer_id=c(20), order_id=o["order_id"])

    # ---- return window boundaries (SD-3, PO-1: 30 days)
    for d, cust in [(29, 23), (30, 25), (31, 26), (1, 27), (45, 28)]:
        o = s.order(c(cust), [("SKU-AP-002", 1)], "delivered", delivered_days_ago=d)
        s.scenario(f"return_window_day_{d}", f"Returnable item delivered {d} days ago", customer_id=c(cust),
                   order_id=o["order_id"])

    # ---- damage report window boundaries (PO-3: 7 days)
    for d, cust in [(6, 29), (7, 31), (8, 32)]:
        o = s.order(c(cust), [("SKU-HM-005", 1)], "delivered", delivered_days_ago=d)
        s.scenario(f"damage_window_day_{d}", f"Dinner set (breakable, 2999) delivered {d} days ago",
                   customer_id=c(cust), order_id=o["order_id"])

    # ---- damaged / wrong item remedies (PO-3)
    o = s.order(c(34), [("SKU-HM-002", 1)], "delivered", delivered_days_ago=2)
    s.scenario("damaged_low_value_in_stock", "Bedsheet 1299 (below photo threshold), in stock -> replacement",
               customer_id=c(34), order_id=o["order_id"])
    o = s.order(c(35), [("SKU-HM-001", 1)], "delivered", delivered_days_ago=3)
    s.scenario("damaged_high_value_in_stock", "Mixer 3999 (photo required), in stock -> replacement",
               customer_id=c(35), order_id=o["order_id"])
    o = s.order(c(36), [("SKU-HM-003", 1)], "delivered", delivered_days_ago=2)
    s.scenario("damaged_out_of_stock", "Cookware 5499 out of stock -> refund incl. shipping", customer_id=c(36),
               order_id=o["order_id"])
    o = s.order(c(37), [("SKU-BK-001", 1)], "delivered", delivered_days_ago=2)
    s.scenario("damaged_with_shipping_fee", "Book 499 + 79 shipping; damaged -> refund 578 if out of stock "
               "(in stock -> replacement)", customer_id=c(37), order_id=o["order_id"])
    o = s.order(c(38), [("SKU-AP-001", 2)], "delivered", delivered_days_ago=1)
    s.scenario("wrong_item", "T-shirts x2 delivered yesterday; customer says wrong item", customer_id=c(38),
               order_id=o["order_id"])

    # ---- non-returnable categories (SD-4, PO-1)
    for cat, sku, cust in [("perishable", "SKU-PR-002", 39), ("personalised", "SKU-PS-001", 40),
                           ("hygiene", "SKU-HY-001", 42)]:
        o = s.order(c(cust), [(sku, 1)], "delivered", delivered_days_ago=4)
        s.scenario(f"non_returnable_{cat}", f"{cat} item delivered 4 days ago", customer_id=c(cust),
                   order_id=o["order_id"])

    # ---- high value (escalation threshold 50000)
    o = s.order(c(43), [("SKU-EL-004", 1)], "delivered", delivered_days_ago=3)
    s.scenario("high_value_delivered", "Laptop 64999 delivered 3 days ago (VIP customer)", customer_id=c(43),
               order_id=o["order_id"])
    o = s.order(c(44), [("SKU-EL-008", 1)], "in_transit", placed_days_ago=3)
    s.scenario("high_value_in_transit", "TV 52999 in transit, on time (VIP customer)", customer_id=c(44), order_id=o["order_id"])
    o = s.order(c(45), [("SKU-EL-003", 2)], "delivered", delivered_days_ago=5)
    s.scenario("high_value_multi_qty", "2 x phone = 49998 (just under 50000 threshold)", customer_id=c(45),
               order_id=o["order_id"])

    # ---- multi-item and partially returned (SD-4)
    o = s.order(c(46), [("SKU-AP-002", 1), ("SKU-AP-001", 2), ("SKU-BK-002", 1)], "delivered", delivered_days_ago=8)
    s.scenario("multi_item_delivered", "Jeans + 2 T-shirts + book, delivered 8 days ago", customer_id=c(46),
               order_id=o["order_id"])
    o = s.order(c(48), [("SKU-AP-003", 1), ("SKU-AP-005", 1)], "delivered", delivered_days_ago=15)
    s.return_(o, [(1, 1)], "refunded", created_days_ago=10, reason="size_issue")
    s.refund(o, o["items"][1]["unit_price"], "processed", created_days_ago=6, processed_days_ago=4,
             reason="return_received")
    s.scenario("partially_returned", "Shoes + kurta; kurta already returned and refunded", customer_id=c(48),
               order_id=o["order_id"])
    o1 = s.order(c(49), [("SKU-HM-004", 1)], "delivered", delivered_days_ago=4, placed_days_ago=9)
    o2 = s.order(c(49), [("SKU-HM-002", 1)], "in_transit", placed_days_ago=9, promised_in_days=-1,
                 last_update_days_ago=2)
    s.scenario("split_shipment", "Two orders placed the same day: lamp delivered, bedsheet in transit and late",
               customer_id=c(49), order_ids=[o1["order_id"], o2["order_id"]])

    # ---- payments (SD-5)
    o = s.order(c(50), [("SKU-EL-005", 1)], "in_transit", placed_days_ago=4, promised_in_days=-1,
                last_update_days_ago=1, payment="card")
    s.charge(o, o["total"], "succeeded", at=rel(-3.99), note="Duplicate capture")
    s.scenario("late_and_double_charged", "Smartwatch 8999 late AND charged twice", customer_id=c(50),
               order_id=o["order_id"])
    o = s.order(c(52), [("SKU-BT-002", 1)], "delivered", delivered_days_ago=6, payment="card")
    s.charge(o, o["total"], "succeeded", at=rel(-(_days(o["placed_at"]) - 0.01)), note="Duplicate capture")
    s.scenario("duplicate_charge", "Hair dryer charged twice", customer_id=c(52), order_id=o["order_id"])
    o = s.order(c(53), [("SKU-AP-005", 1)], "shipped", payment="card", payment_outcome="failed_then_paid")
    s.scenario("failed_then_retried", "First card payment failed, retry succeeded", customer_id=c(53),
               order_id=o["order_id"])
    o = s.order(c(54), [("SKU-EL-006", 1)], "placed", payment="upi", payment_outcome="failed")
    s.scenario("payment_failed_pending", "UPI payment failed; order placed, unpaid", customer_id=c(54),
               order_id=o["order_id"])

    # ---- identity (FR-7..FR-11)
    o = s.order(c(2), [("SKU-EL-001", 1)], "in_transit")
    s.scenario("secondary_email_owner", "Customer has two registered emails; writes from the second",
               customer_id=c(2), order_id=o["order_id"], email="priya.p.shop@example.in")
    o = s.order(c(55), [("SKU-EL-007", 1)], "in_transit")
    s.scenario("single_recent_order", "Customer with exactly one recent order (no order no. needed)",
               customer_id=c(55), order_id=o["order_id"])
    ids = [s.order(c(9), [(sku, 1)], st)["order_id"] for sku, st in
           [("SKU-AP-001", "in_transit"), ("SKU-HM-004", "shipped"), ("SKU-BK-001", "delivered")]]
    s.scenario("several_recent_orders", "Three recent orders; ambiguous without an order number",
               customer_id=c(9), order_ids=ids)
    s.scenario("unknown_sender", "Sender address not registered to any customer",
               email="stranger.unknown@example.net")
    s.scenario("not_owner", "CUST-0005 writes about CUST-0011's order", customer_id=c(5),
               order_id=s.scenarios["status_delivered"]["order_id"], owner_id=c(11))
    s.scenario("customer_no_orders", "Registered customer with no orders", customer_id=c(47))

    # ---- VIP, cancellation, refund status, repeat contact
    o = s.order(c(1), [("SKU-AP-004", 1)], "in_transit")
    s.scenario("vip_in_transit", "VIP customer, order in transit", customer_id=c(1), order_id=o["order_id"])
    o = s.order(c(7), [("SKU-HM-006", 1)], "placed")
    s.scenario("cancel_before_ship", "Placed, not shipped -> cancellable", customer_id=c(7),
               order_id=o["order_id"])
    o = s.order(c(14), [("SKU-AP-002", 1)], "packed")
    s.scenario("cancel_packed", "Packed, not shipped -> cancellable", customer_id=c(14),
               order_id=o["order_id"])
    o = s.order(c(21), [("SKU-EL-006", 1)], "shipped")
    s.scenario("cancel_after_ship", "Already shipped -> not cancellable", customer_id=c(21), order_id=o["order_id"])

    o = s.order(c(24), [("SKU-AP-003", 1)], "in_transit", placed_days_ago=6, promised_in_days=-1,
                last_update_days_ago=1)
    s.customer(c(24))["contact_history"] = [
        {"at": rel(-2), "channel": "email", "order_id": o["order_id"], "topic": "order_status",
         "summary": "Asked where the order is; told it is in transit."},
    ]
    s.scenario("repeat_contact_second", "Second contact about a late order (1 prior contact 2 days ago)",
               customer_id=c(24), order_id=o["order_id"])
    o = s.order(c(30), [("SKU-HM-001", 1)], "in_transit", placed_days_ago=9, promised_in_days=-3,
                last_update_days_ago=2)
    s.customer(c(30))["contact_history"] = [
        {"at": rel(-5), "channel": "email", "order_id": o["order_id"], "topic": "order_status",
         "summary": "Asked for delivery status."},
        {"at": rel(-2), "channel": "phone", "order_id": o["order_id"], "topic": "order_status",
         "summary": "Called about delay; promised update within 48h."},
    ]
    s.scenario("repeat_contact_third", "Third contact about the same late order (VIP)", customer_id=c(30),
               order_id=o["order_id"])
    s.customer(c(33))["contact_history"] = [
        {"at": rel(-40), "channel": "email", "order_id": None, "topic": "product_question",
         "summary": "Asked about jacket sizing."},
    ]

    # ---- filler to widen coverage (SD-2: >= 150 orders, every status several times)
    statuses = ["placed", "packed", "shipped", "in_transit", "out_for_delivery", "delivered", "delivered",
                "delivered", "delivered", "cancelled", "return_in_progress", "returned", "refunded"]
    scenario_customers = {v["customer_id"] for v in s.scenarios.values() if "customer_id" in v}
    free_pool = [cu["customer_id"] for cu in s.customers if cu["customer_id"] not in scenario_customers]
    never_filler = {s.scenarios[n]["customer_id"] for n in
                    ("customer_no_orders", "single_recent_order", "several_recent_orders")}
    old_pool = sorted(scenario_customers - never_filler)
    while len(s.orders) < 170:
        items = [(sku, RNG.choice([1, 1, 1, 2])) for sku in RNG.sample(FILLER_SKUS, RNG.choice([1, 1, 1, 2, 3]))]
        if RNG.random() < 0.3:
            # history for scenario customers: delivered long ago, outside every policy window
            s.order(RNG.choice(old_pool), items, "delivered", delivered_days_ago=RNG.randint(90, 300))
            continue
        status = RNG.choice(statuses)
        kw: dict[str, Any] = {}
        if status == "in_transit":
            kind = RNG.choice(["on_time", "on_time", "delayed", "lost"])
            if kind == "delayed":
                kw = {"placed_days_ago": 8, "promised_in_days": -2, "last_update_days_ago": 1}
            elif kind == "lost":
                kw = {"placed_days_ago": 16, "promised_in_days": -9, "last_update_days_ago": RNG.randint(8, 12)}
        o = s.order(RNG.choice(free_pool), items, status, **kw)
        if status == "cancelled" and o["payment_method"]["type"] != "cod":
            s.refund(o, o["total"], "processed", created_days_ago=_days(o["cancelled_at"]) - 0.1,
                     processed_days_ago=max(0.5, _days(o["cancelled_at"]) - 3), reason="order_cancelled")
        elif status in {"return_in_progress", "returned", "refunded"}:
            delivered = _days(o["delivered_at"])
            created = max(0.5, delivered - RNG.randint(1, 5))
            rstatus = {"return_in_progress": "authorised", "returned": "received", "refunded": "refunded"}[status]
            s.return_(o, [(i, l["qty"]) for i, l in enumerate(o["items"])], rstatus, created_days_ago=created,
                      reason=RNG.choice(["size_issue", "changed_mind", "not_as_expected"]))
            if status != "return_in_progress":
                refund_amount = o["subtotal"]  # shipping not refunded for ordinary returns (PO-2)
                if status == "refunded":
                    s.refund(o, refund_amount, "processed", created_days_ago=max(0.4, created - 1),
                             processed_days_ago=max(0.2, created - 3), reason="return_received")
                else:
                    s.refund(o, refund_amount, "initiated", created_days_ago=0.5, reason="return_received")
    return s


def _days(relstr: str) -> float:
    """'@now-12.5d' -> 12.5 (days ago)."""
    return -float(relstr.removeprefix("@now").removesuffix("d") or 0)


def main() -> None:
    s = build()
    OUT.mkdir(exist_ok=True)
    files = {
        "catalog.json": {"products": list(PRODUCTS.values())},
        "customers.json": {"customers": s.customers},
        "orders.json": {"orders": s.orders},
        "payments.json": {"charges": s.charges, "events": s.payment_events},
        "returns.json": {"returns": s.returns},
        "refunds.json": {"refunds": s.refunds},
        "scenarios.json": {"scenarios": s.scenarios},
    }
    for name, body in files.items():
        (OUT / name).write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    by_status: dict[str, int] = {}
    for o in s.orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    print(f"customers={len(s.customers)} orders={len(s.orders)} charges={len(s.charges)} "
          f"returns={len(s.returns)} refunds={len(s.refunds)} scenarios={len(s.scenarios)}")
    print("orders by status:", dict(sorted(by_status.items())))


if __name__ == "__main__":
    main()
