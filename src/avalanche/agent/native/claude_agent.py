"""Claude Agent SDK native header, turn and live-event parts, ``claude_agent.messages/v1``.

The common layer already carries each turn's message, thinking and tool calls; these
models hold what only the SDK reports, mirrored here without importing the SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, NonNegativeInt

from ..kinds import CommonEventKind, RuntimeKind

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
    common_kinds: ClassVar[Mapping[str, CommonEventKind | None]]

    @property
    def turn(self) -> int | None:
        return None

    @property
    def error(self) -> str | None:
        return None


class Started(_Event):
    """The prompt Avalanche sent: the step's inputs and its working directory's role."""

    kind: Literal["started"] = "started"
    common_kinds = MappingProxyType({"started": None})

    model: str | None
    inputs: JsonValue


class SessionInitialized(_Event):
    """The CLI's own report of the session it started."""

    kind: Literal["session.initialized"] = "session.initialized"
    common_kinds = MappingProxyType({"session.initialized": None})

    session_id: str | None
    model: str | None
    tools: tuple[str, ...]


class ToolUsed(_Event):
    """The model asked for a tool; the common event names the call."""

    kind: Literal["tool.used"] = "tool.used"
    common_kinds = MappingProxyType({"tool.used": "tool.started"})

    turn_index: NonNegativeInt

    @property
    def turn(self) -> int | None:
        return self.turn_index


class ToolReturned(_Event):
    """A tool's result reached the model; the common event says whether it failed."""

    kind: Literal["tool.returned"] = "tool.returned"
    common_kinds = MappingProxyType({"tool.returned": "tool.finished"})

    turn_index: NonNegativeInt

    @property
    def turn(self) -> int | None:
        return self.turn_index


class MessageFinished(_Event):
    """One assistant message, completed by its tool results; the turn is common."""

    kind: Literal["message.finished"] = "message.finished"
    common_kinds = MappingProxyType({"message.finished": "turn.finished"})


class RateLimited(_Event):
    kind: Literal["rate_limit"] = "rate_limit"
    common_kinds = MappingProxyType({"rate_limit": None})

    status: str
    limit_type: str | None


class Finished(_Event):
    """The SDK's result message: how the session ended and the structured output."""

    kind: Literal["finished"] = "finished"
    common_kinds = MappingProxyType({"finished": None})

    subtype: str
    is_error: bool
    num_turns: NonNegativeInt
    structured_output: JsonValue
    # True when the output exceeded the trace size limit and is left out here.
    output_omitted: bool
    # Workspace output fields; Avalanche returns the working directory for them, so
    # they are not part of Claude's structured output.
    workspace_outputs: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def error(self) -> str | None:
        return ("; ".join(self.errors) or self.subtype) if self.is_error else None


ClaudeAgentNativeEvent = Annotated[
    Started
    | SessionInitialized
    | ToolUsed
    | ToolReturned
    | MessageFinished
    | RateLimited
    | Finished,
    Field(discriminator="kind"),
]
