"""MS-2 Order service: orders by id / by customer, items, status, dates, tracking; product lookup.

Reports raw fulfilment status. "Delayed" and "lost" are derived by router code (PO-4).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, Header
from pydantic import BaseModel

from mocks.base import MockService, ServiceSpec, api_error

# Fulfilment states in which the warehouse can still stop the order.
CANCELLABLE_STATUSES = {"placed", "packed"}


class CancelOrder(BaseModel):
    reason: str
    case_id: str | None = None


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    return {
        "orders": {o["order_id"]: public_order(o) for o in seed["orders"]["orders"]},
        "products": {p["sku"]: p for p in seed["catalog"]["products"]},
    }


def public_order(o: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in o.items() if not k.startswith("_")}


def register(app: FastAPI, svc: MockService) -> None:
    @app.get("/orders/{order_id}")
    def get_order(order_id: str) -> dict[str, Any]:
        order = svc.data["orders"].get(order_id.strip().upper())
        if not order:
            raise api_error(404, "order_not_found", f"no order {order_id}")
        return order

    @app.get("/customers/{customer_id}/orders")
    def by_customer(customer_id: str, placed_within_days: int | None = None) -> list[dict[str, Any]]:
        orders = [o for o in svc.data["orders"].values() if o["customer_id"] == customer_id]
        if placed_within_days is not None:
            cutoff = svc.clock.now() - timedelta(days=placed_within_days)
            orders = [o for o in orders if datetime.fromisoformat(o["placed_at"]) >= cutoff]
        return sorted(orders, key=lambda o: o["placed_at"], reverse=True)

    @app.post("/orders/{order_id}/cancel")
    def cancel(order_id: str, req: CancelOrder = Body(...), idempotency_key: str | None = Header(default=None)):
        """Idempotent cancellation. Refunds for prepaid cancellations are handled by the platform (D-023)."""
        def do() -> dict[str, Any]:
            order = get_order(order_id)
            if order["status"] not in CANCELLABLE_STATUSES:
                raise api_error(409, "not_cancellable", f"order is {order['status']}")
            order["status"] = "cancelled"
            order["cancelled_at"] = svc.clock.now().isoformat(timespec="seconds")
            return {"order_id": order["order_id"], "status": "cancelled", "cancelled_at": order["cancelled_at"],
                    "reason": req.reason, "case_id": req.case_id, "idempotency_key": idempotency_key}

        return svc.idempotent(idempotency_key, {"order_id": order_id, **req.model_dump()}, do)

    @app.get("/products/{sku}")
    def product(sku: str) -> dict[str, Any]:
        p = svc.data["products"].get(sku)
        if not p:
            raise api_error(404, "product_not_found", f"no product {sku}")
        return p


SPEC = ServiceSpec("order", ("orders.json", "catalog.json"), initial_data, register)
