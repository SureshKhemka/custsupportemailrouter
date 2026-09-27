"""MS-4 Refund service: issue refund (idempotent); refund status by order.

Never creates two refunds for one idempotency key, and never refunds more than was paid.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Header
from pydantic import BaseModel, Field

from mocks.base import MockService, ServiceSpec, api_error


class CreateRefund(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    reason: str
    case_id: str | None = None


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    return {"order_totals": {o["order_id"]: o["total"] for o in seed["orders"]["orders"]},
            "refunds": {r["refund_id"]: r for r in seed["refunds"]["refunds"]}}


def register(app: FastAPI, svc: MockService) -> None:
    def refunded_so_far(order_id: str) -> float:
        return sum(r["amount"] for r in svc.data["refunds"].values()
                   if r["order_id"] == order_id and r["status"] != "failed")

    @app.post("/refunds")
    def create(req: CreateRefund = Body(...), idempotency_key: str | None = Header(default=None)):
        def do() -> dict[str, Any]:
            total = svc.data["order_totals"].get(req.order_id)
            if total is None:
                raise api_error(404, "order_not_found", f"no order {req.order_id}")
            left = total - refunded_so_far(req.order_id)
            if req.amount > left + 1e-9:
                raise api_error(422, "exceeds_refundable", f"requested {req.amount}, refundable {left}")
            rid = svc.next_id("RF")
            refund = {"refund_id": rid, "order_id": req.order_id, "amount": req.amount, "currency": "INR",
                      "status": "initiated", "reason": req.reason, "method": "original_payment_method",
                      "created_at": svc.clock.now().isoformat(timespec="seconds"), "processed_at": None,
                      "idempotency_key": idempotency_key, "case_id": req.case_id}
            svc.data["refunds"][rid] = refund
            return refund

        return svc.idempotent(idempotency_key, req.model_dump(), do)

    @app.get("/refunds/{refund_id}")
    def get(refund_id: str) -> dict[str, Any]:
        r = svc.data["refunds"].get(refund_id)
        if not r:
            raise api_error(404, "refund_not_found", f"no refund {refund_id}")
        return r

    @app.get("/refunds")
    def by_order(order_id: str) -> list[dict[str, Any]]:
        return sorted((r for r in svc.data["refunds"].values() if r["order_id"] == order_id),
                      key=lambda r: r["created_at"])


SPEC = ServiceSpec("refund", ("orders.json", "refunds.json"), initial_data, register)
