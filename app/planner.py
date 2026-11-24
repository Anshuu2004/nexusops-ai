"""Turns a natural-language request into a validated execution plan.

`RulePlanner` is deterministic and always available. `GeminiPlanner` asks an LLM
for a JSON plan and validates it with Pydantic; `FallbackPlanner` falls back to the
rule planner if the LLM call fails or returns invalid output.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError


class Plan(BaseModel):
    intent: Literal["delayed_order_refund", "unsupported"]
    create_tickets: bool = True
    send_notifications: bool = True
    min_delay_days: int | None = Field(default=None, ge=0, le=365)


class PlanResult(BaseModel):
    plan: Plan
    planner: str
    tokens_in: int | None = None
    tokens_out: int | None = None


class PlannerError(Exception):
    pass


class Planner(Protocol):
    def plan(self, request: str) -> PlanResult: ...


class RulePlanner:
    name = "rule"

    def plan(self, request: str) -> PlanResult:
        text = request.lower()
        if "delay" not in text or not re.search(r"\border", text):
            return PlanResult(plan=Plan(intent="unsupported"), planner=self.name)
        wants_ticket = "ticket" in text
        wants_notify = any(w in text for w in ("email", "notif", "notify", "message"))
        if not wants_ticket and not wants_notify:
            wants_ticket = wants_notify = True
        m = re.search(r"(?:more than|over|at least)\s+(\d+)\s+days?", text)
        return PlanResult(
            plan=Plan(intent="delayed_order_refund", create_tickets=wants_ticket,
                      send_notifications=wants_notify,
                      min_delay_days=int(m.group(1)) if m else None),
            planner=self.name,
        )


PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": ["delayed_order_refund", "unsupported"]},
        "create_tickets": {"type": "BOOLEAN"},
        "send_notifications": {"type": "BOOLEAN"},
        "min_delay_days": {"type": "INTEGER", "nullable": True},
    },
    "required": ["intent", "create_tickets", "send_notifications"],
}

SYSTEM_PROMPT = (
    "You convert a manager's request into a JSON plan for an order-refund workflow. "
    "Use intent 'delayed_order_refund' only for requests about delayed orders, refunds, tickets or "
    "customer notifications; otherwise use 'unsupported'. Output JSON only."
)


class GeminiPlanner:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash",
                 transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        self.api_key, self.model = api_key, model
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def plan(self, request: str) -> PlanResult:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": request}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": PLAN_SCHEMA,
                                 "temperature": 0},
        }
        try:
            resp = self._http.post(url, params={"key": self.api_key}, json=body)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            plan = Plan.model_validate(json.loads(text))
        except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as exc:
            raise PlannerError(f"gemini planner failed: {type(exc).__name__}") from exc
        usage = data.get("usageMetadata", {})
        return PlanResult(plan=plan, planner=self.name,
                          tokens_in=usage.get("promptTokenCount"),
                          tokens_out=usage.get("candidatesTokenCount"))


class FallbackPlanner:
    name = "fallback"

    def __init__(self, primary: Planner, fallback: Planner | None = None):
        self.primary = primary
        self.fallback = fallback or RulePlanner()

    def plan(self, request: str) -> PlanResult:
        try:
            return self.primary.plan(request)
        except PlannerError:
            result = self.fallback.plan(request)
            result.planner = f"{result.planner}(fallback)"
            return result
