"""Typed agent detail at the operator's JSON boundaries."""

from __future__ import annotations

from typing import Literal

from predict_rlm import RunTrace
from pydantic import BaseModel, ConfigDict, JsonValue


class AgentEvidenceMetadata(BaseModel):
    """The three terminal lifecycle fields Avalanche needs from PredictRLM."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    run_id: str
    complete: bool
    terminal_outcome: Literal["completed", "error", "cancelled"]


class AgentTerminalDetail(BaseModel):
    """Final SDK trace and lifecycle metadata; early failures have no trace."""

    model_config = ConfigDict(strict=True, extra="forbid")

    trace: RunTrace | None
    evidence: AgentEvidenceMetadata


class AgentLifecycleEvent(BaseModel):
    """One sanitized event retained by Avalanche's live event stream."""

    model_config = ConfigDict(strict=True, extra="forbid")

    invocation_id: str
    sequence: int
    event_kind: str
    timestamp_ns: int
    data: dict[str, JsonValue]


class AgentTraceEnvelope(BaseModel):
    """Hydrated or pending detail used by the operator client and inspectors."""

    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal[1]
    invocation_id: str | None
    status: str
    run_id: str | None
    events: list[AgentLifecycleEvent]
    trace: RunTrace | None
    evidence: AgentEvidenceMetadata | None
    error: str | None
