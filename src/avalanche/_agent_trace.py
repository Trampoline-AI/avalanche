"""Agent detail as the operator hands it to clients and inspectors."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .agent.live import AgentLiveEvent, AgentTraceTerminal


class AgentTraceEnvelope(BaseModel):
    """One node's retained agent detail: live events, then the final trace if any.

    ``invocation_id`` names the invocation the final trace belongs to; ``events``
    are that invocation's events once it has finished, and every event before.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    schema_version: Literal[2]
    invocation_id: str | None
    status: str
    events: tuple[AgentLiveEvent, ...]
    terminal: AgentTraceTerminal | None
    error: str | None
