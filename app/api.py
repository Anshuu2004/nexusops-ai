"""FastAPI service: run agent workflows, approve actions, inspect runs."""
from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .auth import AuthService, current_user, require_roles
from .db import Database
from .workflow import WorkflowService


class LoginIn(BaseModel):
    username: str
    password: str


class RunIn(BaseModel):
    request: str = Field(min_length=3, max_length=1000)


class ApprovalIn(BaseModel):
    approve: bool
    order_ids: list[str] | None = None


def create_app(db: Database, service: WorkflowService, auth: AuthService) -> FastAPI:
    app = FastAPI(title="NexusOps AI", version="1.0",
                  description="Agentic workflow engine with human approval and audited execution.")
    app.state.auth = auth

    def owned_run(run_id: str, user: dict) -> dict:
        row = db.fetchone("SELECT * FROM runs WHERE run_id=%s", (run_id,))
        # 404 (not 403) so users cannot probe for other people's run ids
        if not row or (row["owner"] != user["sub"] and user["role"] != "admin"):
            raise HTTPException(status_code=404, detail="run not found")
        return row

    @app.get("/health")
    def health():
        db.fetchone("SELECT 1 AS ok")
        return {"status": "ok"}

    @app.post("/auth/token")
    def login(body: LoginIn):
        return {"access_token": auth.login(body.username, body.password), "token_type": "bearer"}

    @app.post("/auth/logout")
    def logout(user: dict = Depends(current_user)):
        auth.revoke(user)
        return {"status": "logged_out"}

    @app.post("/agents/run", status_code=202)
    def run_agent(body: RunIn, user: dict = Depends(require_roles("manager", "admin"))):
        run_id = uuid.uuid4().hex
        return service.start(run_id, user["sub"], body.request)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, user: dict = Depends(current_user)):
        row = owned_run(run_id, user)
        return {k: row[k] for k in ("run_id", "owner", "request", "status", "proposals", "report")}

    @app.post("/runs/{run_id}/approve")
    def approve(run_id: str, body: ApprovalIn, user: dict = Depends(require_roles("manager", "admin"))):
        owned_run(run_id, user)
        # Atomic claim: only one caller can move a run out of awaiting_approval,
        # so a double-click or replayed request can never execute actions twice.
        claimed = db.execute("UPDATE runs SET status='executing', updated_at=now() "
                             "WHERE run_id=%s AND status='awaiting_approval'", (run_id,))
        if claimed == 0:
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        return service.resume(run_id, {"approved": body.approve, "order_ids": body.order_ids})

    @app.get("/runs/{run_id}/audit")
    def audit(run_id: str, user: dict = Depends(current_user)):
        owned_run(run_id, user)
        rows = db.fetchall("SELECT actor, event, detail, ts FROM audit_log WHERE run_id=%s ORDER BY id", (run_id,))
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    @app.get("/runs/{run_id}/traces")
    def traces(run_id: str, user: dict = Depends(current_user)):
        owned_run(run_id, user)
        return db.fetchall("SELECT node, duration_ms, status, tokens_in, tokens_out FROM traces "
                           "WHERE run_id=%s ORDER BY id", (run_id,))

    return app
