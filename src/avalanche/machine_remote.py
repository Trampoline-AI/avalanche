"""Native-async public HTTP transport for remote machine operations.

The SDK stays independent of Delta packages. Configure the existing API bearer and
scope explicitly, or with DELTA_API_URL/DELTA_TOKEN/DELTA_ORG_ID/DELTA_PROJECT_ID.
No credential is minted, persisted or inferred from a local operator context.
"""

from __future__ import annotations

import json
import os
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, JsonValue

from .http_retry import RetryTransport, failure_summary, retry_budget


class MachineRemoteError(RuntimeError):
    """A remote operation failed; retry sends using the same idempotency key."""


class MachineEventReceipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    receipt_id: str
    key: str
    accepted_at: float
    duplicate: bool
    status: Literal["accepted", "ignored"]


class _Started(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    run_id: str


def _scope(
    api_url: str | None, token: str | None, org: str | None, project: str | None
) -> tuple[str, str]:
    values = [
        api_url or os.environ.get("DELTA_API_URL"),
        token or os.environ.get("DELTA_TOKEN"),
        org or os.environ.get("DELTA_ORG_ID"),
        project or os.environ.get("DELTA_PROJECT_ID"),
    ]
    if not all(values):
        raise MachineRemoteError(
            "Remote machines require api_url, token, org and project "
            "(or DELTA_API_URL, DELTA_TOKEN, DELTA_ORG_ID, DELTA_PROJECT_ID)"
        )
    base, bearer, org_id, project_id = values
    assert (
        base is not None
        and bearer is not None
        and org_id is not None
        and project_id is not None
    )
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MachineRemoteError(
            "api_url must be HTTP(S), without credentials, query or fragment"
        )
    root = base.rstrip("/").removesuffix("/v1")
    return (
        f"{root}/v1/orgs/{quote(org_id, safe='')}"
        f"/projects/{quote(project_id, safe='')}/machines",
        bearer,
    )


class MachineRemoteClient:
    """Reusable native-async transport. Close it on the event loop that owns it."""

    def __init__(self, *, retry_budget_s: float | None = None) -> None:
        budget = retry_budget(retry_budget_s)
        self.http = httpx2.AsyncClient(follow_redirects=False, timeout=300)
        # Preserve HTTPX's environment proxy/TLS setup, then apply one policy to
        # both direct and proxy transports.
        self.http._transport = RetryTransport(self.http._transport, budget)
        self.http._mounts = {
            pattern: RetryTransport(transport, budget) if transport is not None else None
            for pattern, transport in self.http._mounts.items()
        }

    async def __aenter__(self) -> MachineRemoteClient:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.http.aclose()

    async def post(
        self, url: str, token: str, body: bytes, *, idempotency_key: str | None = None
    ) -> bytes:
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self.http.post(url, content=body, headers=headers)
        except httpx2.HTTPError as exc:
            raise MachineRemoteError(
                f"Machine outcome unknown ({type(exc).__name__}); "
                "retry the same instance key and Idempotency-Key"
            ) from exc
        if response.status_code not in {200, 201, 202}:
            raise MachineRemoteError(f"Machine request refused ({failure_summary(response)})")
        return response.content


async def _post(
    url: str,
    token: str,
    body: bytes,
    *,
    idempotency_key: str | None = None,
    retry_budget_s: float | None = None,
) -> bytes:
    async with MachineRemoteClient(retry_budget_s=retry_budget_s) as client:
        return await client.post(url, token, body, idempotency_key=idempotency_key)


async def start(
    machine: str,
    key: tuple[JsonValue, ...],
    input: dict[str, JsonValue],
    *,
    deployment_id: str | None = None,
    api_url: str | None = None,
    token: str | None = None,
    org: str | None = None,
    project: str | None = None,
    retry_budget_s: float | None = None,
) -> str:
    root, bearer = _scope(api_url, token, org, project)
    body = json.dumps(
        {"key": key, "input": input, "deployment_id": deployment_id}, allow_nan=False
    ).encode()
    raw = await _post(
        f"{root}/{quote(machine, safe='')}/instances",
        bearer,
        body,
        retry_budget_s=retry_budget_s,
    )
    return _Started.model_validate_json(raw).run_id


async def send(
    machine: str,
    key: tuple[JsonValue, ...],
    event: str,
    *,
    idempotency_key: str,
    payload: BaseModel | JsonValue = None,
    api_url: str | None = None,
    token: str | None = None,
    org: str | None = None,
    project: str | None = None,
    retry_budget_s: float | None = None,
) -> MachineEventReceipt:
    if (
        not idempotency_key
        or len(idempotency_key) > 256
        or any(ord(c) < 32 for c in idempotency_key)
    ):
        raise ValueError("idempotency_key must contain 1-256 printable characters")
    if not event or event in {".", ".."} or "/" in event:
        raise ValueError("event must be a nonempty path segment")
    root, bearer = _scope(api_url, token, org, project)
    encoded = quote(
        json.dumps(
            key, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ),
        safe="",
    )
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    body = b"" if value is None else json.dumps(value, allow_nan=False).encode()
    raw = await _post(
        f"{root}/{quote(machine, safe='')}/instances/{encoded}/events/{quote(event, safe='')}",
        bearer,
        body,
        idempotency_key=idempotency_key,
        retry_budget_s=retry_budget_s,
    )
    receipt = MachineEventReceipt.model_validate_json(raw)
    if receipt.key != event:
        raise MachineRemoteError("Machine receipt event mismatch")
    return receipt
