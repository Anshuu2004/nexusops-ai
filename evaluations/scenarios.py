"""Deterministic generator for the 100-scenario workflow evaluation.

Expected outcomes are computed here by an ORACLE that is independent of the
workflow code (plain Python rules), and written to workflow_scenarios.json
before any agent run, so results cannot be tuned after the fact.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 2026
MIN_DAYS = {"standard": 5, "premium": 3}

COUNTS = {
    "normal": 30,
    "transient_api_failure": 20,
    "timeout_after_commit": 15,
    "human_rejection": 10,
    "partial_approval": 5,
    "crash_recovery": 10,
    "invalid_data": 5,
    "permanent_api_failure": 5,
}


def _order(rng: random.Random, force_eligible: bool | None = None) -> dict:
    tier = rng.choice(["standard", "premium"])
    need = MIN_DAYS[tier]
    if force_eligible is True:
        days = rng.randint(need, need + 6)
    elif force_eligible is False:
        days = rng.randint(1, need - 1)
    else:
        days = rng.randint(1, 12)
    return {"tier": tier, "days": days, "amount": rng.choice([900, 2500, 7800, 18000, 64000]),
            "address_issue": False}


def _orders(rng, n_total: int, n_eligible: int) -> list[dict]:
    specs = [_order(rng, True) for _ in range(n_eligible)]
    specs += [_order(rng, False) for _ in range(n_total - n_eligible)]
    # a couple of address-issue exclusions among the "would be eligible" ones
    for s in specs[n_eligible:n_eligible + 1]:
        s["address_issue"], s["days"] = True, MIN_DAYS[s["tier"]] + 2
    rng.shuffle(specs)
    return specs


def oracle(specs: list[dict], decision: dict, tickets_fail: bool) -> dict:
    eligible = []
    for i, s in enumerate(specs, start=1):
        oid = f"O{i:04d}"
        email_ok = "@" in s.get("email", "ok@example.com")
        customer_ok = "customer_id" not in s
        if not (email_ok and customer_ok and s["amount"] > 0):
            continue
        if s["address_issue"] or s["days"] < MIN_DAYS[s["tier"]]:
            continue
        eligible.append(oid)
    if not eligible:
        return {"status": "no_eligible_orders", "tickets": [], "notifications": []}
    if not decision["approved"]:
        return {"status": "rejected", "tickets": [], "notifications": []}
    chosen = [o for o in eligible if decision.get("order_ids") is None or o in decision["order_ids"]]
    if tickets_fail:
        return {"status": "completed_with_errors", "tickets": [], "notifications": []}
    return {"status": "completed", "tickets": chosen, "notifications": chosen}


def generate() -> list[dict]:
    rng = random.Random(SEED)
    scenarios: list[dict] = []

    def add(category, specs, rules=None, decision=None, crash_after=None):
        decision = decision or {"approved": True, "order_ids": None}
        tickets_fail = any(r["path"] == "/tickets" and r["mode"] == "always_fail" for r in (rules or []))
        scenarios.append({
            "id": f"S{len(scenarios) + 1:03d}", "category": category, "specs": specs,
            "rules": rules or [], "decision": decision, "crash_after": crash_after,
            "expected": oracle(specs, decision, tickets_fail),
        })

    for _ in range(COUNTS["normal"]):
        add("normal", _orders(rng, rng.randint(3, 8), rng.randint(2, 4)))

    for _ in range(COUNTS["transient_api_failure"]):
        pool = [
            {"method": "GET", "path": "/orders", "mode": "fail_first", "n": rng.randint(1, 2)},
            {"method": "GET", "path": "/customers", "mode": "fail_first", "n": 1},
            {"method": "POST", "path": "/tickets", "mode": "fail_first", "n": rng.randint(1, 2)},
            {"method": "POST", "path": "/notifications", "mode": "fail_first", "n": rng.randint(1, 2)},
        ]
        add("transient_api_failure", _orders(rng, rng.randint(3, 7), rng.randint(2, 4)),
            rules=rng.sample(pool, rng.randint(1, 3)))

    for _ in range(COUNTS["timeout_after_commit"]):
        pool = [{"method": "POST", "path": "/tickets", "mode": "timeout_after_commit", "n": 1},
                {"method": "POST", "path": "/notifications", "mode": "timeout_after_commit", "n": 1}]
        add("timeout_after_commit", _orders(rng, rng.randint(3, 7), rng.randint(2, 4)),
            rules=rng.sample(pool, rng.randint(1, 2)))

    for _ in range(COUNTS["human_rejection"]):
        add("human_rejection", _orders(rng, rng.randint(3, 7), rng.randint(2, 4)),
            decision={"approved": False, "order_ids": None})

    for _ in range(COUNTS["partial_approval"]):
        specs = _orders(rng, rng.randint(4, 8), rng.randint(3, 4))
        elig = [f"O{i:04d}" for i, s in enumerate(specs, 1)
                if s["days"] >= MIN_DAYS[s["tier"]] and not s["address_issue"]]
        add("partial_approval", specs, decision={"approved": True, "order_ids": elig[: max(1, len(elig) // 2)]})

    for _ in range(COUNTS["crash_recovery"]):
        specs = _orders(rng, rng.randint(4, 8), rng.randint(3, 4))
        n_elig = sum(1 for s in specs if s["days"] >= MIN_DAYS[s["tier"]] and not s["address_issue"])
        add("crash_recovery", specs, crash_after=rng.randint(1, 2 * n_elig - 1))

    for _ in range(COUNTS["invalid_data"]):
        specs = _orders(rng, rng.randint(4, 7), rng.randint(3, 4))
        bad = rng.sample(range(len(specs)), 2)
        specs[bad[0]]["email"] = "not-an-email"
        specs[bad[1]]["customer_id"] = "ghost-customer"
        add("invalid_data", specs)

    for _ in range(COUNTS["permanent_api_failure"]):
        add("permanent_api_failure", _orders(rng, rng.randint(3, 6), rng.randint(2, 3)),
            rules=[{"method": "POST", "path": "/tickets", "mode": "always_fail"}])

    assert len(scenarios) == 100
    return scenarios


if __name__ == "__main__":
    out = Path(__file__).parent / "workflow_scenarios.json"
    out.write_text(json.dumps(generate(), indent=1), encoding="utf-8")
    print(f"wrote {out}")
