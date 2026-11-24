"""LangGraph workflow: plan -> fetch -> retrieve policy -> evaluate -> prepare ->
human approval -> idempotent execution -> report.

State is checkpointed in PostgreSQL (when a checkpointer is supplied), so a run
can pause for approval and be resumed, even after a process crash.

`mode` exists so the evaluation can compare against weaker baselines:
  full        retries + idempotency keys + action ledger + checkpoint recovery
  retry_only  retries, but no idempotency/ledger/checkpoints
  naive       no retries, no idempotency/ledger/checkpoints
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .clients import ERPClient, ToolError
from .db import Database, to_json
from .planner import Planner
from .policy_rag import PolicyStore


class SimulatedCrash(RuntimeError):
    """Raised by the chaos hook to simulate the process dying mid-execution."""


class RunState(TypedDict, total=False):
    run_id: str
    actor: str
    request: str
    auto_decision: Optional[dict]
    plan: dict
    planner_used: str
    orders: list
    skipped: list
    rules: dict
    exclusions: list
    decisions: list
    proposals: list
    approval: dict
    results: list
    status: str
    error: str
    report: dict


@dataclass
class Deps:
    db: Database
    erp: ERPClient
    policies: PolicyStore
    planner: Planner
    mode: str = "full"                      # full | retry_only | naive
    crash_after_actions: int | None = None  # chaos hook (tests / evaluation only)
    _crash_fired: bool = False

    @property
    def ledger(self) -> bool:
        return self.mode == "full"


def _node(deps: Deps, name: str):
    """Decorator: trace each node's duration/status into the traces table."""
    def wrap(fn):
        def inner(state: RunState):
            start = time.perf_counter()
            status, tin, tout = "ok", None, None
            try:
                out = fn(state)
                tin, tout = out.pop("_tokens_in", None), out.pop("_tokens_out", None)
                return out
            except Exception:
                status = "error"
                raise
            finally:
                deps.db.execute(
                    "INSERT INTO traces (run_id,node,duration_ms,status,tokens_in,tokens_out) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (state["run_id"], name, (time.perf_counter() - start) * 1000, status, tin, tout),
                )
        inner.__name__ = name
        return inner
    return wrap


def build_graph(deps: Deps, checkpointer=None):
    db = deps.db

    # ---- nodes -------------------------------------------------------------
    def plan_node(state: RunState):
        result = deps.planner.plan(state["request"])
        db.audit(state["run_id"], "agent", "plan_created",
                 {"plan": result.plan.model_dump(), "planner": result.planner})
        out: dict[str, Any] = {"plan": result.plan.model_dump(), "planner_used": result.planner,
                               "_tokens_in": result.tokens_in, "_tokens_out": result.tokens_out}
        if result.plan.intent == "unsupported":
            out["status"] = "unsupported_request"
        return out

    def fetch_orders_node(state: RunState):
        try:
            orders = deps.erp.get_delayed_orders()
        except ToolError as exc:
            db.audit(state["run_id"], "agent", "fetch_orders_failed", {"error": str(exc)})
            return {"status": "failed", "error": str(exc)}
        enriched, skipped = [], []
        for o in orders:
            try:
                cust = deps.erp.get_customer(o["customer_id"])
            except ToolError as exc:
                reason = "customer_not_found" if exc.status == 404 else "customer_lookup_failed"
                skipped.append({"order_id": o["id"], "reason": reason})
                continue
            enriched.append({**o, "customer": cust})
        db.audit(state["run_id"], "agent", "orders_fetched",
                 {"orders": len(orders), "enriched": len(enriched), "skipped": len(skipped)})
        return {"orders": to_json(enriched), "skipped": skipped}

    def retrieve_policy_node(state: RunState):
        tiers = sorted({o["customer"].get("tier") for o in state["orders"] if o["customer"].get("tier")})
        rules = {}
        for tier in tiers:
            rule = deps.policies.refund_rule_for_tier(tier)
            if rule:
                rules[tier] = {k: rule[k] for k in ("policy_id", "min_delay_days", "score")}
        exclusions = [{k: e[k] for k in ("policy_id", "flag")} for e in deps.policies.exclusion_rules()
                      if e.get("flag")]
        db.audit(state["run_id"], "agent", "policy_retrieved", {"rules": rules, "exclusions": exclusions})
        return {"rules": to_json(rules), "exclusions": to_json(exclusions)}

    def evaluate_node(state: RunState):
        decisions, skipped = [], list(state.get("skipped", []))
        override = (state.get("plan") or {}).get("min_delay_days")
        excl_flags = {e["flag"]: e["policy_id"] for e in state.get("exclusions", [])}
        for o in state["orders"]:
            cust, oid = o["customer"], o["id"]
            amount, days = o.get("amount"), o.get("days_delayed")
            if (not cust.get("email") or "@" not in str(cust.get("email"))
                    or not isinstance(amount, (int, float)) or amount <= 0
                    or not isinstance(days, int) or days < 0):
                skipped.append({"order_id": oid, "reason": "invalid_data"})
                continue
            flagged = next((pid for flag, pid in excl_flags.items() if o.get(flag)), None)
            if flagged:
                skipped.append({"order_id": oid, "reason": "excluded_by_policy", "policy_id": flagged})
                continue
            rule = state["rules"].get(cust.get("tier"))
            if not rule:
                skipped.append({"order_id": oid, "reason": "no_applicable_policy"})
                continue
            threshold = max(rule["min_delay_days"], override or 0)
            if days < threshold:
                skipped.append({"order_id": oid, "reason": "below_threshold", "policy_id": rule["policy_id"]})
                continue
            decisions.append({"order_id": oid, "customer_id": cust["id"], "email": cust["email"],
                              "tier": cust["tier"], "days_delayed": days, "amount": amount,
                              "policy_id": rule["policy_id"]})
        db.audit(state["run_id"], "agent", "eligibility_evaluated",
                 {"eligible": len(decisions), "skipped": len(skipped)})
        out: dict[str, Any] = {"decisions": decisions, "skipped": skipped}
        if not decisions:
            out["status"] = "no_eligible_orders"
        return out

    def prepare_node(state: RunState):
        plan = state["plan"]
        proposals = []
        for d in state["decisions"]:
            if plan.get("create_tickets", True):
                proposals.append({
                    "order_id": d["order_id"], "type": "ticket", "depends_on_ticket": False,
                    "payload": {"order_id": d["order_id"], "customer_id": d["customer_id"],
                                "subject": f"Delay refund for order {d['order_id']}",
                                "body": f"Order delayed {d['days_delayed']} days. Eligible under {d['policy_id']}."}})
            if plan.get("send_notifications", True):
                proposals.append({
                    "order_id": d["order_id"], "type": "notification",
                    "depends_on_ticket": bool(plan.get("create_tickets", True)),
                    "payload": {"order_id": d["order_id"], "customer_id": d["customer_id"],
                                "to_email": d["email"], "subject": "Your delayed order",
                                "body": f"We're sorry order {d['order_id']} was delayed; a refund request has been opened."}})
        db.audit(state["run_id"], "agent", "proposals_prepared", {"count": len(proposals)})
        db.audit(state["run_id"], "agent", "approval_requested", {"proposals": len(proposals)})
        return {"proposals": proposals}

    def approval_node(state: RunState):
        if state.get("auto_decision") is not None:      # baseline modes: no checkpointing
            decision = state["auto_decision"]
        else:
            decision = interrupt({"proposals": state["proposals"]})   # pauses the graph here
        db.audit(state["run_id"], state.get("actor", "user"), "approval_decision", decision)
        if not decision.get("approved"):
            return {"approval": decision, "status": "rejected"}
        return {"approval": decision}

    def execute_node(state: RunState):
        run_id = state["run_id"]
        approval = state["approval"]
        allowed = set(approval.get("order_ids") or [p["order_id"] for p in state["proposals"]])
        results, ticket_ok, done_actions = [], {}, 0
        for p in state["proposals"]:
            oid, kind = p["order_id"], p["type"]
            if oid not in allowed:
                continue
            if p["depends_on_ticket"] and not ticket_ok.get(oid):
                results.append({"order_id": oid, "type": kind, "status": "skipped",
                                "error": "ticket not created"})
                continue
            key = f"{run_id}:{oid}:{kind}"
            if deps.ledger:
                row = db.fetchone("SELECT status, external_id FROM actions WHERE run_id=%s AND order_id=%s "
                                  "AND action_type=%s", (run_id, oid, kind))
                if row and row["status"] == "succeeded":
                    results.append({"order_id": oid, "type": kind, "status": "succeeded",
                                    "external_id": row["external_id"], "note": "already done"})
                    if kind == "ticket":
                        ticket_ok[oid] = True
                    continue
                db.execute("INSERT INTO actions (run_id,order_id,action_type,status) VALUES (%s,%s,%s,'pending') "
                           "ON CONFLICT DO NOTHING", (run_id, oid, kind))
            try:
                call = deps.erp.create_ticket if kind == "ticket" else deps.erp.send_notification
                ext, attempts = call(p["payload"], key)
            except ToolError as exc:
                if deps.ledger:
                    db.execute("UPDATE actions SET status='failed', error=%s, attempts=%s, updated_at=now() "
                               "WHERE run_id=%s AND order_id=%s AND action_type=%s",
                               (str(exc), exc.attempts, run_id, oid, kind))
                db.audit(run_id, "agent", "action_failed", {"order_id": oid, "type": kind, "error": str(exc)})
                results.append({"order_id": oid, "type": kind, "status": "failed", "error": str(exc)})
                continue
            if deps.ledger:
                db.execute("UPDATE actions SET status='succeeded', external_id=%s, attempts=%s, updated_at=now() "
                           "WHERE run_id=%s AND order_id=%s AND action_type=%s",
                           (ext["id"], attempts, run_id, oid, kind))
            db.audit(run_id, "agent", "action_succeeded",
                     {"order_id": oid, "type": kind, "external_id": ext["id"], "attempts": attempts})
            results.append({"order_id": oid, "type": kind, "status": "succeeded", "external_id": ext["id"]})
            if kind == "ticket":
                ticket_ok[oid] = True
            done_actions += 1
            if (deps.crash_after_actions is not None and not deps._crash_fired
                    and done_actions >= deps.crash_after_actions):
                deps._crash_fired = True
                raise SimulatedCrash(f"crash after {done_actions} actions")
        return {"results": results}

    def report_node(state: RunState):
        results = state.get("results", [])
        failed = [r for r in results if r["status"] in ("failed", "skipped")]
        status = state.get("status")
        if not status:
            status = "completed_with_errors" if failed else "completed"
        policies = sorted({d["policy_id"] for d in state.get("decisions", [])}
                          | {s["policy_id"] for s in state.get("skipped", []) if s.get("policy_id")})
        report = {
            "status": status,
            "request": state["request"],
            "planner": state.get("planner_used"),
            "eligible_orders": len(state.get("decisions", [])),
            "tickets_created": sum(1 for r in results if r["type"] == "ticket" and r["status"] == "succeeded"),
            "notifications_sent": sum(1 for r in results if r["type"] == "notification" and r["status"] == "succeeded"),
            "failed_actions": failed,
            "skipped_orders": state.get("skipped", []),
            "policies_cited": policies,
            "error": state.get("error"),
        }
        db.audit(state["run_id"], "agent", "run_finished", {"status": status})
        return {"report": to_json(report), "status": status}

    # ---- routing -------------------------------------------------------------
    def stop_or(next_node: str):
        return lambda s: "report" if s.get("status") else next_node

    g = StateGraph(RunState)
    for name, fn in [("plan", plan_node), ("fetch_orders", fetch_orders_node),
                     ("retrieve_policy", retrieve_policy_node), ("evaluate", evaluate_node),
                     ("prepare", prepare_node), ("approval", approval_node),
                     ("execute", execute_node), ("report", report_node)]:
        g.add_node(name, _node(deps, name)(fn))
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", stop_or("fetch_orders"), ["report", "fetch_orders"])
    g.add_conditional_edges("fetch_orders", stop_or("retrieve_policy"), ["report", "retrieve_policy"])
    g.add_edge("retrieve_policy", "evaluate")
    g.add_conditional_edges("evaluate", stop_or("prepare"), ["report", "prepare"])
    g.add_edge("prepare", "approval")
    g.add_conditional_edges("approval", stop_or("execute"), ["report", "execute"])
    g.add_edge("execute", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


class WorkflowService:
    """Starts, resumes and recovers runs, and keeps the `runs` table in sync."""

    def __init__(self, deps: Deps, checkpointer=None):
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)
        self.checkpointed = checkpointer is not None

    @staticmethod
    def _cfg(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _sync(self, run_id: str, result: dict) -> dict:
        db = self.deps.db
        if "__interrupt__" in result:
            proposals = result["__interrupt__"][0].value["proposals"]
            db.execute("UPDATE runs SET status='awaiting_approval', proposals=%s::jsonb, updated_at=now() "
                       "WHERE run_id=%s", (_json(proposals), run_id))
            return {"run_id": run_id, "status": "awaiting_approval", "proposals": proposals}
        report = result.get("report", {})
        db.execute("UPDATE runs SET status=%s, report=%s::jsonb, updated_at=now() WHERE run_id=%s",
                   (result.get("status", "completed"), _json(report), run_id))
        return {"run_id": run_id, "status": result.get("status"), "report": report}

    def start(self, run_id: str, actor: str, request: str, auto_decision: dict | None = None) -> dict:
        self.deps.db.execute(
            "INSERT INTO runs (run_id, owner, request, status) VALUES (%s,%s,%s,'running') "
            "ON CONFLICT (run_id) DO NOTHING", (run_id, actor, request))
        self.deps.db.audit(run_id, actor, "run_started", {"request": request})
        state = {"run_id": run_id, "actor": actor, "request": request, "auto_decision": auto_decision}
        result = self.graph.invoke(state, self._cfg(run_id) if self.checkpointed else None)
        return self._sync(run_id, result)

    def resume(self, run_id: str, decision: dict) -> dict:
        result = self.graph.invoke(Command(resume=decision), self._cfg(run_id))
        return self._sync(run_id, result)

    def recover(self, run_id: str) -> dict:
        """Continue a run whose process died mid-execution, from its last checkpoint."""
        result = self.graph.invoke(None, self._cfg(run_id))
        return self._sync(run_id, result)


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)
