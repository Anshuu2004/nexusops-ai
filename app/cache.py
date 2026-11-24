"""Redis helpers: JSON cache and session store. Works with redis-py or fakeredis."""
from __future__ import annotations

import json
from typing import Any


class Cache:
    def __init__(self, client):
        self.r = client
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_url(cls, url: str) -> "Cache":
        import redis
        return cls(redis.Redis.from_url(url, decode_responses=True))

    # -- JSON cache -----------------------------------------------------------
    def get_json(self, key: str) -> Any | None:
        raw = self.r.get(key)
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(raw)

    def set_json(self, key: str, value: Any, ttl: int = 300) -> None:
        self.r.set(key, json.dumps(value), ex=ttl)

    # -- sessions (JWT id allow-list, so tokens can be revoked) ---------------
    def session_create(self, jti: str, username: str, ttl: int) -> None:
        self.r.set(f"session:{jti}", username, ex=ttl)

    def session_valid(self, jti: str) -> bool:
        return self.r.exists(f"session:{jti}") == 1

    def session_revoke(self, jti: str) -> None:
        self.r.delete(f"session:{jti}")
