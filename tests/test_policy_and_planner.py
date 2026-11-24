import json

import httpx
import pytest

from app.planner import FallbackPlanner, GeminiPlanner, Plan, PlannerError, RulePlanner


# ---- policy retrieval (pgvector) -------------------------------------------
def test_premium_rule_retrieved_for_premium(policies):
    rule = policies.refund_rule_for_tier("premium")
    assert rule["policy_id"] == "P-REF-002" and rule["min_delay_days"] == 3


def test_standard_rule_retrieved_for_standard(policies):
    rule = policies.refund_rule_for_tier("standard")
    assert rule["policy_id"] == "P-REF-001" and rule["min_delay_days"] == 5


def test_unknown_tier_has_no_rule(policies):
    assert policies.refund_rule_for_tier("platinum") is None


def test_kind_filter_excludes_unrelated_policies(policies):
    hits = policies.retrieve("hotel reimbursement for business travel", kind="refund_rule", k=5)
    assert hits and all(h["kind"] == "refund_rule" for h in hits)


def test_unfiltered_retrieval_finds_the_right_distractor(policies):
    hits = policies.retrieve("how many days of paid annual leave do employees get", k=3)
    assert hits[0]["policy_id"] == "P-HR-001"


def test_exclusion_policy_retrieved(policies):
    assert any(e["flag"] == "address_issue" for e in policies.exclusion_rules())


def test_policy_retrieval_is_cached(policies):
    policies.cache.hits = policies.cache.misses = 0
    policies.retrieve("cache probe query about refunds", kind="refund_rule")
    policies.retrieve("cache probe query about refunds", kind="refund_rule")
    assert policies.cache.misses == 1 and policies.cache.hits == 1


# ---- planner -----------------------------------------------------------------
def test_rule_planner_supported_request():
    r = RulePlanner().plan("Find delayed orders, check refund policy, create tickets and email customers")
    assert r.plan.intent == "delayed_order_refund" and r.plan.create_tickets and r.plan.send_notifications


def test_rule_planner_tickets_only():
    r = RulePlanner().plan("Raise support tickets for delayed orders")
    assert r.plan.create_tickets and not r.plan.send_notifications


def test_rule_planner_extracts_min_days():
    r = RulePlanner().plan("Handle orders delayed more than 7 days")
    assert r.plan.min_delay_days == 7


def test_rule_planner_rejects_unrelated_request():
    assert RulePlanner().plan("What's the weather like?").plan.intent == "unsupported"


def _gemini(handler):
    return GeminiPlanner("fake-key", transport=httpx.MockTransport(handler))


def _gemini_reply(payload, usage=None):
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
            "usageMetadata": usage or {"promptTokenCount": 120, "candidatesTokenCount": 30}}
    return httpx.Response(200, json=body)


def test_gemini_planner_parses_validated_plan_and_tokens():
    p = _gemini(lambda req: _gemini_reply({"intent": "delayed_order_refund", "create_tickets": True,
                                           "send_notifications": False}))
    r = p.plan("anything")
    assert r.plan.send_notifications is False and (r.tokens_in, r.tokens_out) == (120, 30)


def test_gemini_invalid_output_is_rejected():
    p = _gemini(lambda req: _gemini_reply({"intent": "launch_missiles", "create_tickets": True,
                                           "send_notifications": True}))
    with pytest.raises(PlannerError):
        p.plan("x")


def test_gemini_http_error_is_a_planner_error():
    p = _gemini(lambda req: httpx.Response(500))
    with pytest.raises(PlannerError):
        p.plan("x")


def test_fallback_planner_uses_rules_when_llm_fails():
    p = FallbackPlanner(_gemini(lambda req: httpx.Response(500)))
    r = p.plan("delayed orders: create tickets")
    assert r.plan.intent == "delayed_order_refund" and r.planner == "rule(fallback)"


def test_plan_schema_rejects_out_of_range():
    with pytest.raises(ValueError):
        Plan(intent="delayed_order_refund", min_delay_days=-5)
