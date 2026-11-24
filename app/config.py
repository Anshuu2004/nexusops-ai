"""Runtime settings, read from environment variables."""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    erp_base_url: str = field(default_factory=lambda: _env("ERP_BASE_URL", "http://localhost:8001"))
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", "dev-only-change-me"))
    jwt_ttl_seconds: int = 3600
    max_retries: int = 3
    retry_backoff: float = 0.2
    request_timeout: float = 5.0
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.0-flash"))
