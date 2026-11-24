"""Production entrypoint: `uvicorn app.main:app` (wired from environment variables)."""
from langgraph.checkpoint.postgres import PostgresSaver

from .api import create_app
from .auth import AuthService, seed_users
from .cache import Cache
from .clients import ERPClient
from .config import Settings
from .db import Database
from .planner import FallbackPlanner, GeminiPlanner, RulePlanner
from .policy_rag import PolicyStore
from .workflow import Deps, WorkflowService


def build_app():
    s = Settings()
    db = Database(s.database_url)
    db.init_schema()
    cache = Cache.from_url(s.redis_url)
    policies = PolicyStore(db, cache)
    policies.ingest()
    # Demo accounts. Change these (and JWT_SECRET) for any real deployment.
    seed_users(db, {"admin": ("admin-demo", "admin"), "manager": ("manager-demo", "manager"),
                    "viewer": ("viewer-demo", "viewer")})
    planner = (FallbackPlanner(GeminiPlanner(s.gemini_api_key, s.gemini_model))
               if s.gemini_api_key else RulePlanner())
    erp = ERPClient(s.erp_base_url, timeout=s.request_timeout, max_retries=s.max_retries,
                    backoff=s.retry_backoff)
    saver = PostgresSaver(db.pool)
    saver.setup()
    service = WorkflowService(Deps(db=db, erp=erp, policies=policies, planner=planner), saver)
    return create_app(db, service, AuthService(db, cache, s.jwt_secret, s.jwt_ttl_seconds))


app = build_app()
