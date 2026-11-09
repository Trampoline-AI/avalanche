"""Host-neutral machine operations and their public JSON wire shapes."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MachineKey = tuple[JsonValue, ...]
JsonObject = dict[str, JsonValue]


class MachineStart(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    run_id: str
    key: list[JsonValue]
    created: bool


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
    visit: int = Field(ge=0)
    version: Literal[1] = 1
    accepts: list[str]
    due: float | None
    finished: bool


class MachineHost(Protocol):
    async def start(self, machine: str, key: MachineKey, input: JsonObject) -> MachineStart: ...

    async def send(
        self,
        machine: str,
        key: MachineKey,
        event: str,
        payload: JsonValue,
        idempotency_key: str,
    ) -> MachineEventReceipt: ...


class MachineOpSpec(BaseModel):
    """Frozen operation metadata; arguments use ordinary node input bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    machine: str
    op: Literal["start", "send"]
