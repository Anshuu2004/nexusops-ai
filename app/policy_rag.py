"""Policy retrieval (RAG) on PostgreSQL + pgvector, with Redis caching.

Refund policies are stored as chunks with machine-readable rule fields
(tier, min_delay_days, flag). The agent retrieves the applicable policy by
semantic search, applies metadata filtering (kind), then uses the retrieved
rule and cites its policy id in the final report.
"""
from __future__ import annotations

import threading

from .cache import Cache
from .db import Database

POLICIES = [
    # --- refund rules the workflow depends on --------------------------------
    dict(policy_id="P-REF-001", kind="refund_rule", tier="standard", min_delay_days=5, flag=None,
         title="Standard customer delayed-order refund",
         body="For standard-tier customers, an order delayed by 5 or more days is eligible for a "
              "refund of the shipping fee. A support ticket must be raised for each eligible order."),
    dict(policy_id="P-REF-002", kind="refund_rule", tier="premium", min_delay_days=3, flag=None,
         title="Premium customer delayed-order refund",
         body="Premium-tier customers are eligible for a full refund when an order is delayed by 3 "
              "or more days. A support ticket must be raised and the customer notified."),
    dict(policy_id="P-EXC-001", kind="exclusion", tier=None, min_delay_days=None, flag="address_issue",
         title="Delay refund exclusions",
         body="Orders delayed because of an incorrect or incomplete delivery address supplied by the "
              "customer are not eligible for any delay refund."),
    # --- unrelated policies (retrieval distractors) --------------------------
    dict(policy_id="P-TRV-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Domestic travel reimbursement",
         body="Employees may claim up to 4,500 per night for hotel accommodation on domestic business travel."),
    dict(policy_id="P-IT-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Laptop refresh cycle",
         body="Company laptops are replaced every 36 months or earlier if repair cost exceeds half the price."),
    dict(policy_id="P-HR-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Annual leave entitlement",
         body="Full-time employees receive 24 days of paid annual leave, accrued monthly."),
    dict(policy_id="P-SEC-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Password policy",
         body="Passwords must be at least 12 characters and rotated every 90 days; multi-factor authentication is mandatory."),
    dict(policy_id="P-FIN-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Expense approval limits",
         body="Expenses above 25,000 require approval from a department head; above 100,000 from the finance director."),
    dict(policy_id="P-VND-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Vendor onboarding",
         body="New vendors must complete a security questionnaire and provide tax registration documents before onboarding."),
    dict(policy_id="P-DAT-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Data retention",
         body="Customer records are retained for seven years after account closure, then securely deleted."),
    dict(policy_id="P-INC-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Incident escalation",
         body="Severity-1 incidents must be escalated to the on-call engineer within 15 minutes."),
    dict(policy_id="P-WFH-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Remote work",
         body="Employees may work remotely up to three days per week with manager approval."),
    dict(policy_id="P-SHP-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Shipping carriers",
         body="Standard shipments use regional carriers; express shipments use air freight with tracking."),
    dict(policy_id="P-RET-001", kind="other", tier=None, min_delay_days=None, flag=None,
         title="Product returns",
         body="Customers may return unused products within 30 days of delivery for store credit."),
]

_model = None
_model_lock = threading.Lock()


def embed(texts: list[str]) -> list[list[float]]:
    """Embed with a small local ONNX model (BAAI/bge-small-en-v1.5, 384-dim)."""
    global _model
    with _model_lock:
        if _model is None:
            from fastembed import TextEmbedding
            _model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return [[float(x) for x in v] for v in _model.embed(texts)]


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class PolicyStore:
    def __init__(self, db: Database, cache: Cache | None = None):
        self.db = db
        self.cache = cache

    def ingest(self) -> int:
        texts = [f"{p['title']}. {p['body']}" for p in POLICIES]
        vectors = embed(texts)
        for p, v in zip(POLICIES, vectors):
            self.db.execute(
                """INSERT INTO policy_chunks (policy_id,title,body,kind,tier,min_delay_days,flag,embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)
                   ON CONFLICT (policy_id) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body,
                   kind=EXCLUDED.kind, tier=EXCLUDED.tier, min_delay_days=EXCLUDED.min_delay_days,
                   flag=EXCLUDED.flag, embedding=EXCLUDED.embedding""",
                (p["policy_id"], p["title"], p["body"], p["kind"], p["tier"],
                 p["min_delay_days"], p["flag"], _vec(v)),
            )
        return len(POLICIES)

    def retrieve(self, query: str, kind: str | None = None, k: int = 3) -> list[dict]:
        cache_key = f"policy:{kind}:{k}:{query}"
        if self.cache:
            hit = self.cache.get_json(cache_key)
            if hit is not None:
                return hit
        qv = _vec(embed([query])[0])
        rows = self.db.fetchall(
            """SELECT policy_id,title,body,kind,tier,min_delay_days,flag,
                      1 - (embedding <=> %s::vector) AS score
               FROM policy_chunks
               WHERE (%s::text IS NULL OR kind = %s::text)
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (qv, kind, kind, qv, k),
        )
        rows = [{**r, "score": float(r["score"])} for r in rows]
        if self.cache:
            self.cache.set_json(cache_key, rows, ttl=600)
        return rows

    def refund_rule_for_tier(self, tier: str) -> dict | None:
        """Retrieve refund rules, then pick the highest-ranked one that applies to this tier."""
        hits = self.retrieve(f"delayed order refund eligibility for {tier} tier customers",
                             kind="refund_rule", k=3)
        for h in hits:
            if h["tier"] == tier:
                return h
        return None

    def exclusion_rules(self) -> list[dict]:
        return self.retrieve("orders not eligible for delay refund exclusions", kind="exclusion", k=3)
