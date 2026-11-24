"""Run the 100 scenarios against three workflow modes and write results.

    python -m evaluations.run_eval
"""
from __future__ import annotations

import json
import statistics
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import fakeredis
import pgserver
from langgraph.checkpoint.postgres import PostgresSaver

from app.cache import Cache
from app.db import Database
from app.harness import build_state, make_service
from app.policy_rag import PolicyStore
from app.workflow import SimulatedCrash

from .scenarios import generate

HERE = Path(__file__).parent
REQUEST = "Find delayed orders, check the refund policy, create support tickets and email affected customers"
MODES = ["naive", "retry_only", "full"]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def run_scenario(sc, mode, db, cache, saver, policies):
    state = build_state(sc["specs"])
    svc = make_service(db, cache, state, rules=sc["rules"], mode=mode, saver=saver,
                       crash_after=sc["crash_after"], policies=policies)
    run_id, decision = uuid.uuid4().hex, sc["decision"]
    t0 = time.perf_counter()
    error = None
    try:
        if mode == "full":
            first = svc.start(run_id, "eval", REQUEST)
            try:
                out = svc.resume(run_id, decision) if first["status"] == "awaiting_approval" else first
            except SimulatedCrash:
                out = svc.recover(run_id)                       # resume from Postgres checkpoint
        else:
            try:
                out = svc.start(run_id, "eval", REQUEST, auto_decision=decision)
            except SimulatedCrash:                              # no checkpoint: only option is to rerun
                out = svc.start(uuid.uuid4().hex, "eval", REQUEST, auto_decision=decision)
    except Exception as exc:                                    # an unexpected crash is a failure
        out, error = {"status": f"exception:{type(exc).__name__}"}, repr(exc)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    exp = sc["expected"]
    got_t = sorted(t["order_id"] for t in state.tickets)
    got_n = sorted(n["order_id"] for n in state.notifications)
    exp_t, exp_n = sorted(exp["tickets"]), sorted(exp["notifications"])
    dup_t = len(got_t) - len(set(got_t))
    dup_n = len(got_n) - len(set(got_n))
    ok = out.get("status") == exp["status"] and got_t == exp_t and got_n == exp_n
    return {"id": sc["id"], "category": sc["category"], "passed": ok, "status": out.get("status"),
            "expected_status": exp["status"], "duplicate_actions": dup_t + dup_n,
            "tickets": len(got_t), "expected_tickets": len(exp_t), "elapsed_ms": elapsed_ms,
            "http_attempts": svc.deps.erp.total_attempts, "error": error}


def main():
    scenarios = generate()
    (HERE / "workflow_scenarios.json").write_text(json.dumps(scenarios, indent=1), encoding="utf-8")

    server = pgserver.get_server(tempfile.mkdtemp(prefix="nexusops_eval_"))
    db = Database(server.get_uri())
    db.init_schema()
    saver = PostgresSaver(db.pool)
    saver.setup()
    cache = Cache(fakeredis.FakeRedis(decode_responses=True))
    policies = PolicyStore(db, cache)
    policies.ingest()

    results: dict[str, dict] = {}
    for mode in MODES:
        rows = [run_scenario(sc, mode, db, cache, saver, policies) for sc in scenarios]
        by_cat: dict[str, list[bool]] = defaultdict(list)
        for r in rows:
            by_cat[r["category"]].append(r["passed"])
        lat = [r["elapsed_ms"] for r in rows]
        results[mode] = {
            "scenarios": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "pass_rate_pct": round(100 * sum(r["passed"] for r in rows) / len(rows), 1),
            "duplicate_actions_total": sum(r["duplicate_actions"] for r in rows),
            "scenarios_with_duplicates": sum(1 for r in rows if r["duplicate_actions"]),
            "unexpected_exceptions": sum(1 for r in rows if r["error"]),
            "latency_ms_p50": round(statistics.median(lat), 1),
            "latency_ms_p95": round(pct(lat, 95), 1),
            "by_category": {c: {"passed": sum(v), "total": len(v)} for c, v in by_cat.items()},
            "failures": [{k: r[k] for k in ("id", "category", "status", "expected_status",
                                            "duplicate_actions", "tickets", "expected_tickets")}
                         for r in rows if not r["passed"]][:12],
        }
        print(mode, results[mode]["passed"], "/", len(rows), "dups:", results[mode]["duplicate_actions_total"])

    counts = Counter(s["category"] for s in scenarios)
    out = {
        "benchmark": "NexusOps workflow reliability (fault-injection suite)",
        "seed": 2026, "scenario_count": len(scenarios), "categories": dict(counts),
        "policy_cache": {"hits": cache.hits, "misses": cache.misses},
        "modes": results,
        "notes": ["Synthetic scenarios against an in-process mock ERP; expected outcomes come from an "
                  "independent oracle in scenarios.py, generated before any run.",
                  "naive/retry_only are weaker baselines implemented in this repo, not third-party systems.",
                  "The planner used is the deterministic RulePlanner; no LLM accuracy is measured here."],
    }
    (HERE / "benchmark_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    write_report(out)
    db.close()
    server.cleanup()


def write_report(out: dict) -> None:
    cats = list(out["categories"])
    lines = ["# NexusOps AI - workflow evaluation report", "",
             f"{out['scenario_count']} fault-injection scenarios, seed {out['seed']}. "
             "Generated by `python -m evaluations.run_eval`; raw numbers in `benchmark_results.json`.", "",
             "## Pass rate (same 100 scenarios for every mode)", "",
             "| Mode | Passed | Pass rate | Duplicate actions | p50 / p95 latency |", "|---|---|---|---|---|"]
    desc = {"naive": "naive (no retries, no idempotency, no checkpoints)",
            "retry_only": "retry only (no idempotency, no checkpoints)",
            "full": "full engine (retries + idempotency + action ledger + checkpoint recovery)"}
    for m in MODES:
        r = out["modes"][m]
        lines.append(f"| {desc[m]} | {r['passed']}/{r['scenarios']} | {r['pass_rate_pct']}% | "
                     f"{r['duplicate_actions_total']} | {r['latency_ms_p50']} / {r['latency_ms_p95']} ms |")
    lines += ["", "## By category (passed / total)", "", "| Category | " + " | ".join(MODES) + " |",
              "|---|" + "---|" * len(MODES)]
    for c in cats:
        cells = [f"{out['modes'][m]['by_category'][c]['passed']}/{out['modes'][m]['by_category'][c]['total']}"
                 for m in MODES]
        lines.append(f"| {c} | " + " | ".join(cells) + " |")
    lines += ["", "## How a scenario passes", "",
              "Final status, the exact set of tickets created in the mock ERP, and the exact set of "
              "notifications sent must all equal the oracle's expectation. A duplicated ticket or "
              "notification fails the scenario.", "", "## Limitations", ""]
    lines += [f"- {n}" for n in out["notes"]]
    (HERE / "evaluation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
