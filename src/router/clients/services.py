"""Typed clients for each backend service. Base URLs come from config, so real services can replace the mocks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from router.clients.http import CallRecord, ServiceClient
from router.config.models import Settings
from router.schemas.backend import Charge, Customer, Order, Product, Refund, ReturnAuth


class CustomerClient(ServiceClient):
    def by_email(self, email: str) -> Customer | None:
        data = self.request("GET", "/customers/by-email", op="customer_by_email", params={"email": email}, allow_404=True)
        return Customer.model_validate(data) if data else None


class OrderClient(ServiceClient):
    def get(self, order_id: str) -> Order | None:
        data = self.request("GET", f"/orders/{order_id}", op="get_order", allow_404=True)
        return Order.model_validate(data) if data else None

    def by_customer(self, customer_id: str, placed_within_days: int | None = None) -> list[Order]:
        params = {"placed_within_days": placed_within_days} if placed_within_days is not None else None
        data = self.request("GET", f"/customers/{customer_id}/orders", op="orders_by_customer", params=params)
        return [Order.model_validate(o) for o in data]

    def product(self, sku: str) -> Product | None:
        data = self.request("GET", f"/products/{sku}", op="get_product", allow_404=True)
        return Product.model_validate(data) if data else None

    def cancel(self, order_id: str, reason: str, case_id: str, idempotency_key: str) -> dict[str, Any]:
        return self.request("POST", f"/orders/{order_id}/cancel", op="cancel_order",
                            body={"reason": reason, "case_id": case_id}, idempotency_key=idempotency_key)


class ReturnsClient(ServiceClient):
    def by_order(self, order_id: str) -> list[ReturnAuth]:
        return [ReturnAuth.model_validate(r) for r in
                self.request("GET", "/returns", op="returns_by_order", params={"order_id": order_id})]

    def create(self, order_id: str, lines: list[tuple[str, int]], reason: str, case_id: str,
               idempotency_key: str) -> dict[str, Any]:
        body = {"order_id": order_id, "items": [{"line_id": l, "qty": q} for l, q in lines], "reason": reason,
                "case_id": case_id}
        return self.request("POST", "/returns", op="create_return", body=body, idempotency_key=idempotency_key)


class RefundClient(ServiceClient):
    def by_order(self, order_id: str) -> list[Refund]:
        return [Refund.model_validate(r) for r in
                self.request("GET", "/refunds", op="refunds_by_order", params={"order_id": order_id})]

    def create(self, order_id: str, amount: float, reason: str, case_id: str, idempotency_key: str) -> dict[str, Any]:
        body = {"order_id": order_id, "amount": amount, "reason": reason, "case_id": case_id}
        return self.request("POST", "/refunds", op="issue_refund", body=body, idempotency_key=idempotency_key)


class PaymentsClient(ServiceClient):
    def by_order(self, order_id: str) -> list[Charge]:
        data = self.request("GET", f"/orders/{order_id}/payments", op="payments_by_order", allow_404=True)
        return [Charge.model_validate(c) for c in data["charges"]] if data else []


class ReplacementClient(ServiceClient):
    def create(self, order_id: str, lines: list[tuple[str, int]], reason: str, case_id: str,
               idempotency_key: str) -> dict[str, Any]:
        body = {"order_id": order_id, "items": [{"line_id": l, "qty": q} for l, q in lines], "reason": reason,
                "case_id": case_id}
        return self.request("POST", "/replacements", op="create_replacement", body=body, idempotency_key=idempotency_key)


class OutboxClient(ServiceClient):
    def send(self, case_id: str, to: str, subject: str, body: str, language: str, in_reply_to: str | None,
             idempotency_key: str) -> dict[str, Any]:
        msg = {"case_id": case_id, "to": to, "subject": subject, "body": body, "language": language,
               "in_reply_to": in_reply_to}
        return self.request("POST", "/messages", op="send_reply", body=msg, idempotency_key=idempotency_key)


@dataclass
class Backends:
    customer: CustomerClient
    order: OrderClient
    returns: ReturnsClient
    refund: RefundClient
    payments: PaymentsClient
    replacement: ReplacementClient
    outbox: OutboxClient

    @classmethod
    def from_config(cls, cfg: Settings, *, on_call: Callable[[CallRecord], None] | None = None,
                    http_clients: dict[str, httpx.Client] | None = None,
                    sleep: Callable[[float], None] | None = None) -> Backends:
        """`http_clients` lets tests plug in in-process transports (e.g. FastAPI TestClient)."""
        kinds = {"customer": CustomerClient, "order": OrderClient, "returns": ReturnsClient, "refund": RefundClient,
                 "payments": PaymentsClient, "replacement": ReplacementClient, "outbox": OutboxClient}
        extra = {"sleep": sleep} if sleep else {}
        return cls(**{name: kind(name, cfg.services[name], client=(http_clients or {}).get(name), on_call=on_call,
                                 **extra)
                      for name, kind in kinds.items()})
