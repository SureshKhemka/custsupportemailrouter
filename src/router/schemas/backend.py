"""Typed views of backend service records. Unknown fields are ignored so real services can add fields."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

OrderStatus = Literal["placed", "packed", "shipped", "in_transit", "out_for_delivery", "delivered", "cancelled",
                      "return_in_progress", "returned", "refunded"]


class _Rec(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ContactRecord(_Rec):
    at: datetime
    channel: str
    order_id: str | None
    topic: str
    summary: str


class Customer(_Rec):
    customer_id: str
    name: str
    emails: list[str]
    phone: str | None = None
    tier: str
    contact_history: list[ContactRecord] = []


class OrderLine(_Rec):
    line_id: str
    sku: str
    name: str
    category: str
    qty: int
    unit_price: float
    returned_qty: int = 0


class TrackingEvent(_Rec):
    at: datetime
    status: str
    location: str
    description: str


class Order(_Rec):
    order_id: str
    customer_id: str
    status: OrderStatus
    items: list[OrderLine]
    subtotal: float
    shipping_fee: float
    total: float
    currency: str
    placed_at: datetime
    shipped_at: datetime | None = None
    promised_delivery_date: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    tracking_events: list[TrackingEvent] = []

    def line(self, line_id: str) -> OrderLine:
        return next(l for l in self.items if l.line_id == line_id)

    @property
    def last_tracking_update(self) -> datetime | None:
        return max((e.at for e in self.tracking_events), default=None)


class Product(_Rec):
    sku: str
    name: str
    category: str
    price: float
    in_stock: bool


class ReturnAuth(_Rec):
    return_id: str
    order_id: str
    status: str
    created_at: datetime


class Refund(_Rec):
    refund_id: str
    order_id: str
    amount: float
    status: Literal["initiated", "processed", "failed"]
    reason: str
    created_at: datetime
    processed_at: datetime | None = None


class Charge(_Rec):
    charge_id: str
    order_id: str
    amount: float
    status: str
    created_at: datetime
