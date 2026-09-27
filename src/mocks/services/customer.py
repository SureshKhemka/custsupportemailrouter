"""MS-1 Customer service: look up by email; profile, tier, registered emails, contact history."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from mocks.base import MockService, ServiceSpec, api_error


def initial_data(seed: dict[str, Any]) -> dict[str, Any]:
    return {"customers": {c["customer_id"]: c for c in seed["customers"]["customers"]}}


def register(app: FastAPI, svc: MockService) -> None:
    def get(customer_id: str) -> dict[str, Any]:
        cust = svc.data["customers"].get(customer_id)
        if not cust:
            raise api_error(404, "customer_not_found", f"no customer {customer_id}")
        return cust

    @app.get("/customers/by-email")
    def by_email(email: str) -> dict[str, Any]:
        wanted = email.strip().lower()
        for cust in svc.data["customers"].values():
            if wanted in (e.lower() for e in cust["emails"]):
                return cust
        raise api_error(404, "customer_not_found", "no customer registered with that email")

    @app.get("/customers/{customer_id}")
    def by_id(customer_id: str) -> dict[str, Any]:
        return get(customer_id)

    @app.get("/customers/{customer_id}/contacts")
    def contacts(customer_id: str) -> list[dict[str, Any]]:
        return get(customer_id)["contact_history"]


SPEC = ServiceSpec("customer", ("customers.json",), initial_data, register)
