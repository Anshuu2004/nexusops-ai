import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import AuthService
from app.harness import build_state, make_service

SECRET = "test-secret-key-that-is-long-enough-for-hs256"
REQ = {"request": "Find delayed orders, create tickets and email customers"}


@pytest.fixture
def env(db, cache, saver, policies, users):
    state = build_state([{"days": 9}, {"days": 7, "tier": "premium"}])
    service = make_service(db, cache, state, saver=saver, policies=policies)
    auth = AuthService(db, cache, SECRET, ttl=3600)
    client = TestClient(create_app(db, service, auth))
    return client, state, auth


def login(client, user):
    r = client.post("/auth/token", json={"username": user, "password": f"pw-{user[:5]}" if user == "alice" else {
        "bob": "pw-bob", "vic": "pw-vic", "root": "pw-root"}[user]})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_health_is_public(env):
    client, *_ = env
    assert client.get("/health").json() == {"status": "ok"}


def test_wrong_password_rejected(env):
    client, *_ = env
    assert client.post("/auth/token", json={"username": "alice", "password": "nope"}).status_code == 401


def test_running_an_agent_requires_a_token(env):
    client, state, _ = env
    assert client.post("/agents/run", json=REQ).status_code == 401
    assert state.tickets == []


def test_garbage_token_rejected(env):
    client, *_ = env
    assert client.post("/agents/run", json=REQ, headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401


def test_viewer_cannot_start_runs(env):
    client, state, _ = env
    assert client.post("/agents/run", json=REQ, headers=login(client, "vic")).status_code == 403
    assert state.tickets == []


def test_manager_full_flow_via_api(env):
    client, state, _ = env
    h = login(client, "alice")
    run = client.post("/agents/run", json=REQ, headers=h).json()
    assert run["status"] == "awaiting_approval" and state.tickets == []
    done = client.post(f"/runs/{run['run_id']}/approve", json={"approve": True}, headers=h).json()
    assert done["status"] == "completed" and len(state.tickets) == 2
    got = client.get(f"/runs/{run['run_id']}", headers=h).json()
    assert got["report"]["tickets_created"] == 2


def test_other_users_cannot_see_or_approve_my_run(env):
    client, state, _ = env
    ha, hb = login(client, "alice"), login(client, "bob")
    run_id = client.post("/agents/run", json=REQ, headers=ha).json()["run_id"]
    assert client.get(f"/runs/{run_id}", headers=hb).status_code == 404
    assert client.get(f"/runs/{run_id}/audit", headers=hb).status_code == 404
    assert client.post(f"/runs/{run_id}/approve", json={"approve": True}, headers=hb).status_code == 404
    assert state.tickets == []


def test_admin_can_inspect_any_run(env):
    client, *_ = env
    run_id = client.post("/agents/run", json=REQ, headers=login(client, "alice")).json()["run_id"]
    assert client.get(f"/runs/{run_id}", headers=login(client, "root")).status_code == 200


def test_viewer_cannot_approve(env):
    client, state, _ = env
    run_id = client.post("/agents/run", json=REQ, headers=login(client, "alice")).json()["run_id"]
    assert client.post(f"/runs/{run_id}/approve", json={"approve": True}, headers=login(client, "vic")).status_code == 403
    assert state.tickets == []


def test_double_approval_is_rejected_and_creates_no_duplicates(env):
    client, state, _ = env
    h = login(client, "alice")
    run_id = client.post("/agents/run", json=REQ, headers=h).json()["run_id"]
    assert client.post(f"/runs/{run_id}/approve", json={"approve": True}, headers=h).status_code == 200
    assert client.post(f"/runs/{run_id}/approve", json={"approve": True}, headers=h).status_code == 409
    assert len(state.tickets) == 2 and len(state.notifications) == 2


def test_logout_revokes_the_token(env):
    client, *_ = env
    h = login(client, "alice")
    assert client.post("/auth/logout", headers=h).status_code == 200
    assert client.post("/agents/run", json=REQ, headers=h).status_code == 401


def test_expired_token_rejected(env):
    client, _, auth = env
    token = auth.issue("alice", "manager", ttl=-10)
    assert client.post("/agents/run", json=REQ, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unknown_run_is_404(env):
    client, *_ = env
    assert client.get("/runs/does-not-exist", headers=login(client, "alice")).status_code == 404


def test_request_validation(env):
    client, *_ = env
    assert client.post("/agents/run", json={"request": "x"}, headers=login(client, "alice")).status_code == 422


def test_traces_endpoint_returns_node_timings(env):
    client, *_ = env
    h = login(client, "alice")
    run_id = client.post("/agents/run", json=REQ, headers=h).json()["run_id"]
    nodes = {t["node"] for t in client.get(f"/runs/{run_id}/traces", headers=h).json()}
    assert {"plan", "fetch_orders", "approval"} <= nodes
