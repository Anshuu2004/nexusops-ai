"""PostgreSQL access (psycopg pool) and schema."""
from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    pw_hash  TEXT NOT NULL,
    role     TEXT NOT NULL CHECK (role IN ('viewer','manager','admin'))
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    request    TEXT NOT NULL,
    status     TEXT NOT NULL,
    proposals  JSONB,
    report     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per external side effect. The UNIQUE key is what makes retries and
-- crash recovery safe: a (run, order, action) can only ever be recorded once.
CREATE TABLE IF NOT EXISTS actions (
    run_id      TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status      TEXT NOT NULL,
    external_id TEXT,
    attempts    INT NOT NULL DEFAULT 0,
    error       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, order_id, action_type)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    actor  TEXT NOT NULL,
    event  TEXT NOT NULL,
    detail JSONB,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_run_idx ON audit_log (run_id);

CREATE TABLE IF NOT EXISTS traces (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node        TEXT NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL,
    tokens_in   INT,
    tokens_out  INT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS traces_run_idx ON traces (run_id);

CREATE TABLE IF NOT EXISTS policy_chunks (
    policy_id      TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    tier           TEXT,
    min_delay_days INT,
    flag           TEXT,
    embedding      vector(384) NOT NULL
);
"""


class Database:
    def __init__(self, url: str, max_size: int = 8):
        self.url = url
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=True,
        )

    def execute(self, sql: str, params: Any = None) -> int:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def fetchall(self, sql: str, params: Any = None) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params: Any = None) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def init_schema(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(SCHEMA, prepare=False)  # multi-statement script

    # -- small helpers used by the workflow -------------------------------
    def audit(self, run_id: str, actor: str, event: str, detail: dict | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log (run_id, actor, event, detail) VALUES (%s,%s,%s,%s)",
            (run_id, actor, event, Jsonb(detail or {})),
        )

    def close(self) -> None:
        self.pool.close()


def to_json(obj: Any) -> Any:
    """Round-trip through json so psycopg/LangGraph only see plain types."""
    return json.loads(json.dumps(obj, default=str))
