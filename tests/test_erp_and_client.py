import httpx
import pytest
from a2wsgi import ASGIMiddleware

from app.clients import ERPClient, ToolError
from app.harness import ChaosTransport, build_state, make_transport
from app.mock_erp import create_erp_app


def client_for(state, rules=None, **kw):
    kw.setdefault("backoff", 0.0)
    return ERPClient("http://erp", transport=make_transport(state, rules), **kw)


def test_mock_erp_lists_only_delayed_orders():
    state = build_state([{"days": 6}, {"days": 2}])
    state.orders[1]["status"] = "shipped"
    assert [o["id"] for o in client_for(state).get_delayed_orders()] == ["O0001"]


def test_mock_erp_unknown_customer_is_404_and_not_retried():
    c = client_for(build_state([{}]))
    with pytest.raises(ToolError) as e:
        c.get_customer("nope")
    assert e.value.status == 404 and e.value.attempts == 1


def test_ticket_idempotency_key_prevents_duplicates():
    state = build_state([{}])
    c = client_for(state)
    payload = {"order_id": "O0001", "customer_id": "C001", "subject": "s"}
    a, _ = c.create_ticket(payload, "key-1")
    b, _ = c.create_ticket(payload, "key-1")
    assert a["id"] == b["id"] and len(state.tickets) == 1


def test_without_idempotency_key_duplicates_are_created():
    state = build_state([{}])
    c = client_for(state, use_idempotency=False)
    payload = {"order_id": "O0001", "customer_id": "C001", "subject": "s"}
    c.create_ticket(payload, "key-1")
    c.create_ticket(payload, "key-1")
    assert len(state.tickets) == 2


def test_retry_recovers_from_transient_503():
    state = build_state([{}])
    rules = [{"method": "GET", "path": "/orders", "mode": "fail_first", "n": 2}]
    assert len(client_for(state, rules).get_delayed_orders()) == 1


def test_retries_exhausted_raises_tool_error():
    state = build_state([{}])
    rules = [{"method": "GET", "path": "/orders", "mode": "always_fail"}]
    c = client_for(state, rules, max_retries=2)
    with pytest.raises(ToolError) as e:
        c.get_delayed_orders()
    assert e.value.attempts == 3


def test_no_retry_mode_fails_on_first_error():
    state = build_state([{}])
    rules = [{"method": "GET", "path": "/orders", "mode": "fail_first", "n": 1}]
    with pytest.raises(ToolError):
        client_for(state, rules, max_retries=0).get_delayed_orders()


def test_timeout_after_commit_with_idempotency_creates_exactly_one_ticket():
    state = build_state([{}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "timeout_after_commit", "n": 1}]
    c = client_for(state, rules)
    c.create_ticket({"order_id": "O0001", "customer_id": "C001", "subject": "s"}, "k")
    assert len(state.tickets) == 1


def test_timeout_after_commit_without_idempotency_duplicates():
    state = build_state([{}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "timeout_after_commit", "n": 1}]
    c = client_for(state, rules, use_idempotency=False)
    c.create_ticket({"order_id": "O0001", "customer_id": "C001", "subject": "s"}, "k")
    assert len(state.tickets) == 2
