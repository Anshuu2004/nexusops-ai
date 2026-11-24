"""HTTP tool client for the ERP: timeouts, bounded retries, idempotency keys."""
from __future__ import annotations

import time
from typing import Any

import httpx


class ToolError(Exception):
    """A tool call failed permanently (non-retryable status or retries exhausted)."""

    def __init__(self, message: str, status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ERPClient:
    def __init__(
        self,
        base_url: str = "http://erp",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
        max_retries: int = 3,
        backoff: float = 0.2,
        use_idempotency: bool = True,
    ):
        self.max_retries = max_retries
        self.backoff = backoff
        self.use_idempotency = use_idempotency
        self.total_attempts = 0
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None,
                 idempotency_key: str | None = None) -> tuple[Any, int]:
        headers = {}
        if idempotency_key and self.use_idempotency:
            headers["Idempotency-Key"] = idempotency_key
        last_error = "unknown error"
        last_status: int | None = None
        attempts = 0
        for attempt in range(self.max_retries + 1):
            attempts = attempt + 1
            self.total_attempts += 1
            try:
                resp = self._http.request(method, path, json=json, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error, last_status = f"transport error: {type(exc).__name__}", None
            else:
                if resp.status_code < 400:
                    return resp.json(), attempts
                last_status = resp.status_code
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ToolError(f"{method} {path}: {last_error}", resp.status_code, attempts)
            if attempt < self.max_retries and self.backoff:
                time.sleep(self.backoff * (2 ** attempt))
        raise ToolError(f"{method} {path} failed after {attempts} attempts: {last_error}",
                        last_status, attempts)

    # -- tools ---------------------------------------------------------------
    def get_delayed_orders(self) -> list[dict]:
        data, _ = self._request("GET", "/orders", params={"status": "delayed"})
        return data

    def get_customer(self, customer_id: str) -> dict:
        data, _ = self._request("GET", f"/customers/{customer_id}")
        return data

    def create_ticket(self, payload: dict, idempotency_key: str | None = None) -> tuple[dict, int]:
        return self._request("POST", "/tickets", json=payload, idempotency_key=idempotency_key)

    def send_notification(self, payload: dict, idempotency_key: str | None = None) -> tuple[dict, int]:
        return self._request("POST", "/notifications", json=payload, idempotency_key=idempotency_key)
