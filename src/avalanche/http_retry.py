"""Native-async retry policy for Delta remote HTTP connection setup."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2

logger = logging.getLogger(__name__)
RETRY_STATUSES = {429, 500, 502, 503, 504}
_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def retry_budget(value: float | None) -> float:
    budget = (
        float(os.environ.get("DELTA_HTTP_RETRY_BUDGET_S", "60")) if value is None else value
    )
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("DELTA_HTTP_RETRY_BUDGET_S must be finite and non-negative")
    return budget


def response_reason(response: httpx2.Response) -> str:
    """Public reason tokens only: provider bodies may contain credentials."""
    try:
        value = response.json()
    except ValueError:
        return "request_failed"
    if isinstance(value, dict):
        detail = value.get("detail", value)
        if isinstance(detail, dict):
            detail = detail.get("reason", detail.get("code"))
        if isinstance(detail, str) and _CODE.fullmatch(detail):
            return detail
    return "request_failed"


def failure_summary(response: httpx2.Response) -> str:
    message = f"HTTP {response.status_code}: {response_reason(response)}"
    if (hint := response.headers.get("Retry-After")) is not None:
        message += f" (Retry-After: {hint})"
    return message


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if date.tzinfo is None:
        return None
    return max(0.0, (date - datetime.now(UTC)).total_seconds())


def repeatable(request: httpx2.Request) -> bool:
    if request.method in {"GET", "HEAD"} or request.headers.get("Idempotency-Key"):
        return True
    # The machine registry permanently binds a canonical key to one run.
    if request.method == "POST" and "/machines/" in request.url.path:
        if request.url.path.endswith("/instances"):
            body = json.loads(request.content)
            return isinstance(body, dict) and isinstance(body.get("key"), list)
    return False


class RetryTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, budget_s: float) -> None:
        self.inner = inner
        self.budget_s = budget_s

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        deadline = time.monotonic() + self.budget_s
        safe = repeatable(request)
        attempt = 1
        while True:
            if self.budget_s:
                remaining = max(0.001, deadline - time.monotonic())
                timeout = request.extensions.get("timeout", {})
                request.extensions["timeout"] = {
                    key: remaining if value is None else min(value, remaining)
                    for key, value in timeout.items()
                }
            response = None
            error = None
            try:
                try:
                    async with asyncio.timeout(
                        max(0, deadline - time.monotonic()) if self.budget_s else None
                    ):
                        response = await self.inner.handle_async_request(request)
                except TimeoutError as exc:
                    raise httpx2.ReadTimeout(
                        "HTTP retry budget exhausted during response setup"
                    ) from exc
            except (
                httpx2.NetworkError,
                httpx2.TimeoutException,
                httpx2.RemoteProtocolError,
            ) as exc:
                if not safe or not self.budget_s:
                    raise
                error = exc
                reason = type(exc).__name__
                delay = None
            else:
                if (
                    not self.budget_s
                    or response.status_code not in RETRY_STATUSES
                    or (response.status_code != 429 and not safe)
                ):
                    return response
                await response.aread()
                reason = f"{response.status_code} {response_reason(response)}"
                delay = retry_after(response.headers.get("Retry-After"))
            if delay is None:
                delay = random.uniform(0, min(10.0, 0.5 * 2 ** min(attempt - 1, 10)))
            remaining = deadline - time.monotonic()
            if remaining <= delay:
                if remaining > 0:
                    await asyncio.sleep(remaining)
                if error is not None:
                    raise error
                assert response is not None
                return response
            if response is not None:
                await response.aclose()
            attempt += 1
            logger.info("retrying in %gs: %s (attempt %d)", delay, reason, attempt)
            await asyncio.sleep(delay)
