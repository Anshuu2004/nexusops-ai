import uuid

import pytest

from app.harness import build_state, make_service
from app.workflow import SimulatedCrash

REQ = "Find delayed orders, check the refund policy, create support tickets and email affected customers"
APPROVE = {"approved": True}


def rid() -> str:
    return uuid.uuid4().hex


def run_full(db, cache, saver, policies, state, decision=APPROVE, **kw):
    svc = make_service(db, cache, state, saver=saver, policies=policies, **kw)
    run_id = rid()
    first = svc.start(run_id, "alice", REQ)
    if first["status"] != "awaiting_approval":
        return svc, run_id, first
    return svc, run_id, svc.resume(run_id, decision)


def test_happy_path_pauses_then_completes(db, cache, saver, policies):
    state = build_state([{"days": 6, "tier": "standard"}, {"days": 4, "tier": "premium"}])
    svc = make_service(db, cache, state, saver=saver, policies=policies)
    run_id = rid()
    first = svc.start(run_id, "alice", REQ)
    assert first["status"] == "awaiting_approval" and len(first["proposals"]) == 4
    assert state.tickets == []                               # nothing happens before approval
    done = svc.resume(run_id, APPROVE)
    assert done["status"] == "completed"
    assert len(state.tickets) == 2 and len(state.notifications) == 2


def test_policy_thresholds_decide_eligibility(db, cache, saver, policies):
    state = build_state([{"days": 4, "tier": "standard"},   # below 5 -> no
                         {"days": 5, "tier": "standard"},   # yes
                         {"days": 3, "tier": "premium"},    # yes
                         {"days": 2, "tier": "premium"}])   # below 3 -> no
    _, _, out = run_full(db, cache, saver, policies, state)
    assert sorted(t["order_id"] for t in state.tickets) == ["O0002", "O0003"]
    assert out["report"]["policies_cited"] == ["P-REF-001", "P-REF-002"]


def test_address_issue_orders_are_excluded_by_policy(db, cache, saver, policies):
    state = build_state([{"days": 9, "address_issue": True}, {"days": 9}])
    _, _, out = run_full(db, cache, saver, policies, state)
    assert [t["order_id"] for t in state.tickets] == ["O0002"]
    assert {"order_id": "O0001", "reason": "excluded_by_policy", "policy_id": "P-EXC-001"} in out["report"]["skipped_orders"]


def test_rejection_performs_no_external_actions(db, cache, saver, policies):
    state = build_state([{"days": 9}, {"days": 8}])
    _, _, out = run_full(db, cache, saver, policies, state, decision={"approved": False})
    assert out["status"] == "rejected"
    assert state.tickets == [] and state.notifications == []


def test_partial_approval_only_executes_selected_orders(db, cache, saver, policies):
    state = build_state([{"days": 9}, {"days": 9}, {"days": 9}])
    _, _, out = run_full(db, cache, saver, policies, state, decision={"approved": True, "order_ids": ["O0002"]})
    assert [t["order_id"] for t in state.tickets] == ["O0002"]
    assert out["status"] == "completed"


def test_no_eligible_orders_skips_approval(db, cache, saver, policies):
    state = build_state([{"days": 1}, {"days": 2}])
    _, _, out = run_full(db, cache, saver, policies, state)
    assert out["status"] == "no_eligible_orders" and state.tickets == []


def test_unsupported_request_is_declined(db, cache, saver, policies):
    svc = make_service(db, cache, build_state([{"days": 9}]), saver=saver, policies=policies)
    out = svc.start(rid(), "alice", "write me a poem")
    assert out["status"] == "unsupported_request"


def test_orders_api_outage_fails_gracefully(db, cache, saver, policies):
    state = build_state([{"days": 9}])
    rules = [{"method": "GET", "path": "/orders", "mode": "always_fail"}]
    _, _, out = run_full(db, cache, saver, policies, state, rules=rules)
    assert out["status"] == "failed" and "orders" in out["report"]["error"]


def test_invalid_and_missing_data_is_skipped_not_fatal(db, cache, saver, policies):
    state = build_state([{"days": 9, "email": "not-an-email"}, {"days": 9, "customer_id": "ghost"}, {"days": 9}])
    _, _, out = run_full(db, cache, saver, policies, state)
    reasons = {s["order_id"]: s["reason"] for s in out["report"]["skipped_orders"]}
    assert reasons == {"O0001": "invalid_data", "O0002": "customer_not_found"}
    assert [t["order_id"] for t in state.tickets] == ["O0003"]


def test_transient_failures_are_retried(db, cache, saver, policies):
    state = build_state([{"days": 9}, {"days": 9}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "fail_first", "n": 2}]
    _, _, out = run_full(db, cache, saver, policies, state, rules=rules)
    assert out["status"] == "completed" and len(state.tickets) == 2


def test_timeout_after_commit_creates_no_duplicates(db, cache, saver, policies):
    state = build_state([{"days": 9}, {"days": 9}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "timeout_after_commit", "n": 1},
             {"method": "POST", "path": "/notifications", "mode": "timeout_after_commit", "n": 1}]
    _, _, out = run_full(db, cache, saver, policies, state, rules=rules)
    assert out["status"] == "completed"
    assert len(state.tickets) == 2 and len(state.notifications) == 2


def test_permanent_ticket_failure_is_reported_and_blocks_notifications(db, cache, saver, policies):
    state = build_state([{"days": 9}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "always_fail"}]
    _, _, out = run_full(db, cache, saver, policies, state, rules=rules)
    assert out["status"] == "completed_with_errors"
    assert state.tickets == [] and state.notifications == []
    assert {f["type"] for f in out["report"]["failed_actions"]} == {"ticket", "notification"}


def test_crash_mid_execution_resumes_from_checkpoint_without_duplicates(db, cache, saver, policies):
    state = build_state([{"days": 9}, {"days": 9}, {"days": 9}])
    svc = make_service(db, cache, state, saver=saver, policies=policies, crash_after=3)
    run_id = rid()
    assert svc.start(run_id, "alice", REQ)["status"] == "awaiting_approval"
    with pytest.raises(SimulatedCrash):
        svc.resume(run_id, APPROVE)
    assert len(state.tickets) == 2 and len(state.notifications) == 1      # partially done
    done = svc.recover(run_id)                                            # continue from checkpoint
    assert done["status"] == "completed"
    assert len(state.tickets) == 3 and len(state.notifications) == 3      # all done, none repeated


def test_recovery_survives_a_brand_new_service_instance(db, cache, saver, policies):
    """Simulates a process restart: a fresh service object resumes from Postgres checkpoints."""
    state = build_state([{"days": 9}, {"days": 9}])
    svc1 = make_service(db, cache, state, saver=saver, policies=policies, crash_after=1)
    run_id = rid()
    svc1.start(run_id, "alice", REQ)
    with pytest.raises(SimulatedCrash):
        svc1.resume(run_id, APPROVE)
    svc2 = make_service(db, cache, state, saver=saver, policies=policies)
    assert svc2.recover(run_id)["status"] == "completed"
    assert len(state.tickets) == 2 and len(state.notifications) == 2


def test_action_ledger_records_each_side_effect_once(db, cache, saver, policies):
    state = build_state([{"days": 9}])
    _, run_id, _ = run_full(db, cache, saver, policies, state)
    rows = db.fetchall("SELECT action_type, status FROM actions WHERE run_id=%s ORDER BY action_type", (run_id,))
    assert [(r["action_type"], r["status"]) for r in rows] == [("notification", "succeeded"), ("ticket", "succeeded")]


def test_audit_log_and_traces_are_written(db, cache, saver, policies):
    state = build_state([{"days": 9}])
    _, run_id, _ = run_full(db, cache, saver, policies, state)
    events = [r["event"] for r in db.fetchall("SELECT event FROM audit_log WHERE run_id=%s ORDER BY id", (run_id,))]
    for expected in ("run_started", "plan_created", "policy_retrieved", "approval_requested",
                     "approval_decision", "action_succeeded", "run_finished"):
        assert expected in events
    nodes = {r["node"] for r in db.fetchall("SELECT node FROM traces WHERE run_id=%s", (run_id,))}
    assert {"plan", "fetch_orders", "retrieve_policy", "evaluate", "approval", "execute", "report"} <= nodes


def test_naive_mode_has_no_retries(db, cache, policies):
    state = build_state([{"days": 9}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "fail_first", "n": 1}]
    svc = make_service(db, cache, state, mode="naive", rules=rules, policies=policies)
    out = svc.start(rid(), "alice", REQ, auto_decision=APPROVE)
    assert out["status"] == "completed_with_errors" and state.tickets == []


def test_retry_only_mode_duplicates_after_timeout_commit(db, cache, policies):
    """Documents the failure the idempotency design prevents."""
    state = build_state([{"days": 9}])
    rules = [{"method": "POST", "path": "/tickets", "mode": "timeout_after_commit", "n": 1}]
    svc = make_service(db, cache, state, mode="retry_only", rules=rules, policies=policies)
    svc.start(rid(), "alice", REQ, auto_decision=APPROVE)
    assert len(state.tickets) == 2
