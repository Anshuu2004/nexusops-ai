"""Mock enterprise REST API (orders, customers, tickets, notifications).

Stands in for the customer-management / ticketing systems the agent integrates
with. It honours an `Idempotency-Key` header on POST, like Stripe-style APIs do.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel


@dataclass
class ERPState:
    orders: list[dict] = field(default_factory=list)
    customers: dict[str, dict] = field(default_factory=dict)
    tickets: list[dict] = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    _idem_tickets: dict[str, dict] = field(default_factory=dict)
    _idem_notifs: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)


class TicketIn(BaseModel):
    order_id: str
    customer_id: str
    subject: str
    body: str = ""


class NotificationIn(BaseModel):
    order_id: str
    customer_id: str
    to_email: str
    subject: str
    body: str = ""


def create_erp_app(state: ERPState) -> FastAPI:
    app = FastAPI(title="Mock ERP", version="1.0")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/orders")
    def list_orders(status: str = Query("delayed")):
        return [o for o in state.orders if o.get("status") == status]

    @app.get("/customers/{customer_id}")
    def get_customer(customer_id: str):
        c = state.customers.get(customer_id)
        if c is None:
            raise HTTPException(status_code=404, detail="customer not found")
        return c

    @app.post("/tickets", status_code=201)
    def create_ticket(body: TicketIn, idempotency_key: str | None = Header(default=None)):
        with state._lock:
            if idempotency_key and idempotency_key in state._idem_tickets:
                return state._idem_tickets[idempotency_key]
            ticket = {"id": f"TCK-{len(state.tickets) + 1:05d}", **body.model_dump()}
            state.tickets.append(ticket)
            if idempotency_key:
                state._idem_tickets[idempotency_key] = ticket
            return ticket

    @app.post("/notifications", status_code=201)
    def send_notification(body: NotificationIn, idempotency_key: str | None = Header(default=None)):
        with state._lock:
            if idempotency_key and idempotency_key in state._idem_notifs:
                return state._idem_notifs[idempotency_key]
            notif = {"id": f"NTF-{len(state.notifications) + 1:05d}", **body.model_dump()}
            state.notifications.append(notif)
            if idempotency_key:
                state._idem_notifs[idempotency_key] = notif
            return notif

    return app


def seed_demo_state(n_orders: int = 12, seed: int = 7) -> ERPState:
    """Deterministic demo data used by the standalone mock service."""
    rng = random.Random(seed)
    state = ERPState()
    for i in range(1, n_orders + 1):
        cid = f"C{i:03d}"
        state.customers[cid] = {
            "id": cid,
            "name": f"Customer {i}",
            "email": f"customer{i}@example.com",
            "tier": rng.choice(["standard", "premium"]),
        }
        state.orders.append(
            {
                "id": f"O{i:04d}",
                "customer_id": cid,
                "amount": rng.choice([1200, 4500, 8900, 15000, 52000]),
                "days_delayed": rng.randint(1, 9),
                "status": "delayed",
                "address_issue": rng.random() < 0.15,
            }
        )
    return state


# Module-level app for `uvicorn app.mock_erp:app` (docker-compose mock-erp service).
app = create_erp_app(seed_demo_state())
