"""Agent detail as the operator hands it to clients and inspectors."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .agent.live import AgentLiveEvent, AgentTraceTerminal


class AgentTraceEnvelope(BaseModel):
    """One node's retained agent detail: live events, then the final event if any.

    ``invocation_id`` names the invocation the final event belongs to; ``events``
    are that invocation's events once it has finished, and every event before.
    The client builds it in process from bodies the operator serves one by one, so
    it carries no schema version of its own.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    invocation_id: str | None
    status: str
    events: tuple[AgentLiveEvent, ...]
    terminal: AgentTraceTerminal | None
    error: str | None
