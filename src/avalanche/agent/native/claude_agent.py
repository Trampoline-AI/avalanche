"""Claude Agent SDK native header, turn and live-event parts, ``claude_agent.messages/v1``.

The common layer already carries the run's inputs and outputs and each turn's message,
thinking and tool calls; these models hold what only the SDK reports, mirrored here
without importing the SDK.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from ..kinds import RuntimeKind

_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)

FORMAT = "claude_agent.messages/v1"


class ClaudeAgentTurn(BaseModel):
    """The assistant message behind one turn, as the SDK reports it."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "claude_agent"

    format: Literal["claude_agent.messages/v1"] = FORMAT
    message_id: str | None
    model: str
    stop_reason: str | None
    api_error: str | None

    @property
    def failed(self) -> bool:
        return self.api_error is not None


class ClaudeAgentTraceHeader(BaseModel):
    """Claude Agent SDK session details that have no common-header equivalent."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "claude_agent"

    format: Literal["claude_agent.messages/v1"] = FORMAT
    # The SDK's result subtype, such as ``success`` or ``error_max_turns``.
    result_subtype: str | None
    # Turns as the CLI counts them, which include tool-result round trips.
    num_turns: NonNegativeInt
    duration_api_ms: NonNegativeInt | None
    permission_denials: NonNegativeInt
    errors: tuple[str, ...]


class _Event(BaseModel):
    model_config = _STRICT

    format: Literal["claude_agent.messages/v1"] = FORMAT
    runtime_kind: ClassVar[RuntimeKind] = "claude_agent"

    @property
    def turn(self) -> int | None:
        return None

    @property
    def error(self) -> str | None:
        return None


class SessionInitialized(_Event):
    """The CLI's own report of the session it started."""

    kind: Literal["session.initialized"] = "session.initialized"

    session_id: str | None
    model: str | None
    tools: tuple[str, ...]


class RateLimited(_Event):
    kind: Literal["rate_limit"] = "rate_limit"

    status: str
    limit_type: str | None


ClaudeAgentNativeEvent = Annotated[
    SessionInitialized | RateLimited,
    Field(discriminator="kind"),
]
