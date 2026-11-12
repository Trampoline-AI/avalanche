"""Host-neutral machine operations and their public JSON wire shapes."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MachineKey = tuple[JsonValue, ...]
JsonObject = dict[str, JsonValue]

# Native File/Workspace models and nested references survive until host admission.
type MachinePayload = (
    BaseModel
    | None
    | bool
    | int
    | float
    | str
    | list[MachinePayload]
    | dict[str, MachinePayload]
)


def payload_to_json(payload: MachinePayload) -> JsonValue:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, dict):
        return {name: payload_to_json(value) for name, value in payload.items()}
    if isinstance(payload, list):
        return [payload_to_json(value) for value in payload]
    return payload


class MachineEventReceipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    receipt_id: str
    key: str
    accepted_at: float
    duplicate: bool
    status: Literal["accepted", "ignored"]


class MachineInstance(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    machine: str
    key: list[JsonValue]
    run_id: str
    deployment_id: str | None = None
    status: str
    state: str | None
    entry: int = Field(ge=0)
    version: Literal[1] = 1
    accepts: list[str]
    due: float | None
    finished: bool


class MachineHost(Protocol):
    async def send(
        self,
        machine: str,
        key: MachineKey | str,
        event: str,
        payload: MachinePayload,
        idempotency_key: str,
    ) -> MachineEventReceipt: ...


class MachineOpSpec(BaseModel):
    """Frozen operation metadata; arguments use ordinary node input bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    machine: str
    op: Literal["send"] = "send"
