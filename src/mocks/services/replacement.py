"""MS-6 Replacement service: create a replacement shipment (idempotent). Rejects out-of-stock items."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import Body, FastAPI, Header
from pydantic import BaseModel, Field

from mocks.base import MockService, ServiceSpec, api_error


class ReplacementLine(BaseModel):
    line_id: str
    qty: int = Field(gt=0)


class CreateReplacement(BaseModel):
    order_id: str
    items: list[ReplacementLine] = Field(min_length=1)
    reason: str
    case_id: str | None = None


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    return {
        "orders": {o["order_id"]: {l["line_id"]: {"sku": l["sku"], "qty": l["qty"]} for l in o["items"]}
                   for o in seed["orders"]["orders"]},
        "stock": {p["sku"]: p["in_stock"] for p in seed["catalog"]["products"]},
        "replacements": {},
    }


def register(app: FastAPI, svc: MockService) -> None:
    @app.post("/replacements")
    def create(req: CreateReplacement = Body(...), idempotency_key: str | None = Header(default=None)):
        def do() -> dict[str, Any]:
            lines = svc.data["orders"].get(req.order_id)
            if lines is None:
                raise api_error(404, "order_not_found", f"no order {req.order_id}")
            items = []
            for line in req.items:
                ol = lines.get(line.line_id)
                if not ol:
                    raise api_error(422, "unknown_line", f"{line.line_id} is not on {req.order_id}")
                if line.qty > ol["qty"]:
                    raise api_error(422, "quantity_exceeds_ordered", f"{line.line_id}: ordered {ol['qty']}")
                if not svc.data["stock"].get(ol["sku"], False):
                    raise api_error(422, "out_of_stock", f"{ol['sku']} is out of stock")
                items.append({"line_id": line.line_id, "sku": ol["sku"], "qty": line.qty})
            now = svc.clock.now()
            rid = svc.next_id("RPL")
            rec = {"replacement_id": rid, "order_id": req.order_id, "items": items, "reason": req.reason,
                   "status": "created", "created_at": now.isoformat(timespec="seconds"),
                   "ships_by": (now + timedelta(days=1)).isoformat(timespec="seconds"),
                   "idempotency_key": idempotency_key, "case_id": req.case_id}
            svc.data["replacements"][rid] = rec
            return rec

        return svc.idempotent(idempotency_key, req.model_dump(), do)

    @app.get("/replacements")
    def by_order(order_id: str) -> list[dict[str, Any]]:
        return [r for r in svc.data["replacements"].values() if r["order_id"] == order_id]


SPEC = ServiceSpec("replacement", ("orders.json", "catalog.json"), initial_data, register)
