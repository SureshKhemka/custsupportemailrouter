"""MS-3 Returns service: create return authorisation (idempotent); get return status.

Validates only facts it owns (order lines, quantities left to return). Policy eligibility
(window, non-returnable categories) is decided by router code, not here.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import Body, FastAPI, Header
from pydantic import BaseModel, Field

from mocks.base import MockService, ServiceSpec, api_error

ACTIVE = {"authorised", "picked_up", "received", "refunded"}


class ReturnLine(BaseModel):
    line_id: str
    qty: int = Field(gt=0)


class CreateReturn(BaseModel):
    order_id: str
    items: list[ReturnLine] = Field(min_length=1)
    reason: str
    case_id: str | None = None


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    orders = {o["order_id"]: {"customer_id": o["customer_id"],
                              "lines": {l["line_id"]: {"sku": l["sku"], "qty": l["qty"]} for l in o["items"]}}
              for o in seed["orders"]["orders"]}
    return {"orders": orders, "returns": {r["return_id"]: r for r in seed["returns"]["returns"]}}


def register(app: FastAPI, svc: MockService) -> None:
    def already_returned(order_id: str, line_id: str) -> int:
        return sum(i["qty"] for r in svc.data["returns"].values()
                   if r["order_id"] == order_id and r["status"] in ACTIVE
                   for i in r["items"] if i["line_id"] == line_id)

    @app.post("/returns")
    def create(req: CreateReturn = Body(...), idempotency_key: str | None = Header(default=None)):
        def do() -> dict[str, Any]:
            order = svc.data["orders"].get(req.order_id)
            if not order:
                raise api_error(404, "order_not_found", f"no order {req.order_id}")
            items = []
            for line in req.items:
                ol = order["lines"].get(line.line_id)
                if not ol:
                    raise api_error(422, "unknown_line", f"{line.line_id} is not on {req.order_id}")
                left = ol["qty"] - already_returned(req.order_id, line.line_id)
                if line.qty > left:
                    raise api_error(422, "quantity_exceeds_returnable",
                                    f"{line.line_id}: requested {line.qty}, returnable {left}")
                items.append({"line_id": line.line_id, "sku": ol["sku"], "qty": line.qty})
            now = svc.clock.now()
            rid = svc.next_id("RA")
            ret = {"return_id": rid, "order_id": req.order_id, "items": items, "reason": req.reason,
                   "status": "authorised", "created_at": now.isoformat(timespec="seconds"),
                   "pickup_scheduled_for": (now + timedelta(days=2)).isoformat(timespec="seconds"),
                   "idempotency_key": idempotency_key, "case_id": req.case_id}
            svc.data["returns"][rid] = ret
            return ret

        return svc.idempotent(idempotency_key, req.model_dump(), do)

    @app.get("/returns/{return_id}")
    def get(return_id: str) -> dict[str, Any]:
        r = svc.data["returns"].get(return_id)
        if not r:
            raise api_error(404, "return_not_found", f"no return {return_id}")
        return r

    @app.get("/returns")
    def by_order(order_id: str) -> list[dict[str, Any]]:
        return sorted((r for r in svc.data["returns"].values() if r["order_id"] == order_id),
                      key=lambda r: r["created_at"])


SPEC = ServiceSpec("returns", ("orders.json", "returns.json"), initial_data, register)
