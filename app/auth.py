"""JWT authentication, role-based authorization and Redis-backed sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid

import jwt
from fastapi import Depends, HTTPException, Request

from .cache import Cache
from .db import Database

ROLES = ("viewer", "manager", "admin")
_ITERATIONS = 120_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split(":")
    candidate = hash_password(password, bytes.fromhex(salt_hex)).split(":")[1]
    return hmac.compare_digest(candidate, digest_hex)


def seed_users(db: Database, users: dict[str, tuple[str, str]]) -> None:
    """users: {username: (password, role)}"""
    for name, (password, role) in users.items():
        db.execute(
            "INSERT INTO users (username, pw_hash, role) VALUES (%s,%s,%s) "
            "ON CONFLICT (username) DO UPDATE SET pw_hash=EXCLUDED.pw_hash, role=EXCLUDED.role",
            (name, hash_password(password), role))


class AuthService:
    def __init__(self, db: Database, cache: Cache, secret: str, ttl: int = 3600):
        self.db, self.cache, self.secret, self.ttl = db, cache, secret, ttl

    def login(self, username: str, password: str) -> str:
        row = self.db.fetchone("SELECT pw_hash, role FROM users WHERE username=%s", (username,))
        if not row or not verify_password(password, row["pw_hash"]):
            raise HTTPException(status_code=401, detail="invalid credentials")
        return self.issue(username, row["role"])

    def issue(self, username: str, role: str, ttl: int | None = None) -> str:
        ttl = self.ttl if ttl is None else ttl
        jti = uuid.uuid4().hex
        now = int(time.time())
        token = jwt.encode({"sub": username, "role": role, "jti": jti, "iat": now, "exp": now + ttl},
                           self.secret, algorithm="HS256")
        self.cache.session_create(jti, username, max(ttl, 1))
        return token

    def verify(self, token: str) -> dict:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="invalid or expired token")
        if not self.cache.session_valid(claims["jti"]):
            raise HTTPException(status_code=401, detail="session revoked")
        return claims

    def revoke(self, claims: dict) -> None:
        self.cache.session_revoke(claims["jti"])


def current_user(request: Request) -> dict:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return request.app.state.auth.verify(header[7:])


def require_roles(*allowed: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(status_code=403, detail="insufficient role")
        return user
    return dep
