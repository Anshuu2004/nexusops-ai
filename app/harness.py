"""Test/evaluation harness: in-process ERP + fault injection ("chaos") transport.

Not used in production. Both the pytest suite and the evaluation runner build
their stacks from here so they exercise exactly the same code paths.
"""
from __future__ import annotations

from collections import defaultdict

import httpx
from a2wsgi import ASGIMiddleware

from .cache import Cache
from .clients import ERPClient
from .db import Database
from .mock_erp import ERPState, create_erp_app
from .planner import Planner, RulePlanner
from .policy_rag import PolicyStore
from .workflow import Deps, WorkflowService


class ChaosTransport(httpx.BaseTransport):
    """Wraps a transport and injects failures.

    rule = {"method": "POST", "path": "/tickets", "mode": <mode>, "n": 1}
      fail_first            first n attempts of each distinct request get HTTP 503 (never reach the server)
      timeout_after_commit  first n attempts of each distinct request reach the server (side effect is
                            committed) but the client sees a ReadTimeout, forcing a retry
      always_fail           every attempt gets HTTP 503
    """

    def __init__(self, inner: httpx.BaseTransport, rules: list[dict]):
        self.inner, self.rules = inner, rules
        self._seen: dict[tuple, int] = defaultdict(int)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        for i, rule in enumerate(self.rules):
            if rule["method"] != request.method or not request.url.path.startswith(rule["path"]):
                continue
            key = (i, request.method, request.url.path, body)
            self._seen[key] += 1
            attempt = self._seen[key]
            mode = rule["mode"]
            if mode == "always_fail":
                return httpx.Response(503, request=request)
            if attempt <= rule.get("n", 1):
                if mode == "fail_first":
                    return httpx.Response(503, request=request)
                if mode == "timeout_after_commit":
                    self.inner.handle_request(request).close()  # server processes it ...
                    raise httpx.ReadTimeout("simulated timeout", request=request)  # ... client never hears
        return self.inner.handle_request(request)


def make_transport(state: ERPState, rules: list[dict] | None = None) -> httpx.BaseTransport:
    inner = httpx.WSGITransport(app=ASGIMiddleware(create_erp_app(state)))
    return ChaosTransport(inner, rules) if rules else inner


def make_service(db: Database, cache: Cache, state: ERPState, *, rules: list[dict] | None = None,
                 mode: str = "full", saver=None, crash_after: int | None = None,
                 planner: Planner | None = None, policies: PolicyStore | None = None) -> WorkflowService:
    client = ERPClient("http://erp", transport=make_transport(state, rules),
                       max_retries=0 if mode == "naive" else 3, backoff=0.0,
                       use_idempotency=(mode == "full"))
    deps = Deps(db=db, erp=client, policies=policies or PolicyStore(db, cache),
                planner=planner or RulePlanner(), mode=mode, crash_after_actions=crash_after)
    return WorkflowService(deps, saver if mode == "full" else None)


def build_state(specs: list[dict]) -> ERPState:
    """specs: dicts with keys days, tier, amount, address_issue, email (all optional)."""
    state = ERPState()
    for i, s in enumerate(specs, start=1):
        cid = f"C{i:03d}"
        state.customers[cid] = {"id": cid, "name": f"Customer {i}",
                                "email": s.get("email", f"c{i}@example.com"), "tier": s.get("tier", "standard")}
        state.orders.append({"id": f"O{i:04d}", "customer_id": s.get("customer_id", cid),
                             "amount": s.get("amount", 5000), "days_delayed": s.get("days", 6),
                             "status": "delayed", "address_issue": s.get("address_issue", False)})
    return state
