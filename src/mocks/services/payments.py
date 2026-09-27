"""MS-5 Payments service: charges and payment events per order (read-only)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from mocks.base import MockService, ServiceSpec, api_error


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    by_order: dict[str, dict[str, list]] = {o["order_id"]: {"charges": [], "events": []}
                                            for o in seed["orders"]["orders"]}
    for ch in seed["payments"]["charges"]:
        by_order[ch["order_id"]]["charges"].append(ch)
    for ev in seed["payments"]["events"]:
        by_order[ev["order_id"]]["events"].append(ev)
    return {"by_order": by_order}


def register(app: FastAPI, svc: MockService) -> None:
    @app.get("/orders/{order_id}/payments")
    def payments(order_id: str) -> dict[str, Any]:
        rec = svc.data["by_order"].get(order_id)
        if rec is None:
            raise api_error(404, "order_not_found", f"no payment records for {order_id}")
        return {"order_id": order_id,
                "charges": sorted(rec["charges"], key=lambda c: c["created_at"]),
                "events": sorted(rec["events"], key=lambda e: e["at"])}


SPEC = ServiceSpec("payments", ("orders.json", "payments.json"), initial_data, register)
