"""Agent evidence on the wire: live events and the one terminal trace per invocation.

While an agent runs, every event it reports crosses to the operator as an
``AgentLiveEvent``: Avalanche's sequence number and timestamp, the common event
when there is one, and the runtime's native payload. The invocation then ends
with one ``AgentTraceFinished`` or ``AgentTraceUnavailable``. The final trace
carries no turns, since they already travelled as ``turn.finished`` events.

Nothing here imports DSPy or a runtime SDK, so the operator, client and TUI
validate every body without the runtime extras installed.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
    model_validator,
)

from .kinds import RuntimeKind
from .native import NativeEvent
from .trace import AgentTrace, AgentTraceStatus, Turn

# Serialized bodies always carry defaulted fields such as ``kind``; their
# exported JSON Schema says so.
_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)


class TurnFinished(BaseModel):
    """A finished turn.

    A turn too large for one live event drops some of its tool calls; the omitted
    count says how many, so viewers can show that rather than hide it.
    """

    model_config = _STRICT

    kind: Literal["turn.finished"] = "turn.finished"
    turn: Turn
    omitted_tool_calls: NonNegativeInt

    @property
    def tool_call_count(self) -> int:
        return self.omitted_tool_calls + len(self.turn.tool_calls)


class ToolStarted(BaseModel):
    model_config = _STRICT

    kind: Literal["tool.started"] = "tool.started"
    call_id: str
    name: str


class ToolFinished(BaseModel):
    model_config = _STRICT

    kind: Literal["tool.finished"] = "tool.finished"
    call_id: str
    name: str
    error: str | None


AgentEvent = Annotated[
    TurnFinished | ToolStarted | ToolFinished,
    Field(discriminator="kind"),
]


class AgentLiveEvent(BaseModel):
    """One live event of an agent invocation, as the operator stores and serves it."""

    model_config = _STRICT

    kind: Literal["event"] = "event"
    schema_version: Literal[2] = 2
    invocation_id: str = Field(min_length=1)
    sequence: PositiveInt
    timestamp_ns: NonNegativeInt
    runtime_kind: RuntimeKind
    common: AgentEvent | None
    native: NativeEvent

    @model_validator(mode="after")
    def _check_kinds(self) -> Self:
        native = self.native
        if native.runtime_kind != self.runtime_kind:
            raise ValueError(
                f"native format {native.format!r} does not belong to "
                f"runtime {self.runtime_kind!r}"
            )
        expected = native.common_kinds[native.kind]
        actual = self.common.kind if self.common is not None else None
        if actual != expected:
            raise ValueError(
                f"native event {native.kind!r} travels with common event "
                f"{expected!r}, not {actual!r}"
            )
        if isinstance(self.common, TurnFinished) and self.common.turn.native.format != (
            native.format
        ):
            raise ValueError(
                f"turn of format {self.common.turn.native.format!r} does not belong to "
                f"native format {native.format!r}"
            )
        return self

    @property
    def turn(self) -> int | None:
        match self.common:
            case TurnFinished(turn=turn):
                return turn.index
        return self.native.turn

    @property
    def error(self) -> str | None:
        match self.common:
            case ToolFinished(error=error) if error is not None:
                return error
        return self.native.error

    @property
    def failed(self) -> bool:
        """Whether the event reports a failure, with or without an error text.

        A turn fails as its runtime decides (failed code, an API error); the agent
        may have handled a failed tool call inside it.
        """
        failed_turn = isinstance(self.common, TurnFinished) and self.common.turn.failed
        return self.error is not None or failed_turn


class AgentTraceFinished(BaseModel):
    """The final trace of an invocation; its turns travelled as live events.

    ``complete`` says whether every live event of the invocation was delivered.
    """

    model_config = _STRICT

    kind: Literal["trace_finished"] = "trace_finished"
    schema_version: Literal[2] = 2
    invocation_id: str = Field(min_length=1)
    record: AgentTrace
    complete: bool

    @model_validator(mode="after")
    def _check_streamed(self) -> Self:
        if not self.record.turns_streamed:
            raise ValueError("a final trace on the wire must have streamed its turns")
        return self


class AgentTraceUnavailable(BaseModel):
    """An invocation that ended without a trace the runtime could export."""

    model_config = _STRICT

    kind: Literal["trace_unavailable"] = "trace_unavailable"
    schema_version: Literal[2] = 2
    invocation_id: str = Field(min_length=1)
    runtime_kind: RuntimeKind
    status: AgentTraceStatus
    error: str
    external_run_id: str | None
    complete: bool


AgentTraceTerminal = Annotated[
    AgentTraceFinished | AgentTraceUnavailable, Field(discriminator="kind")
]
AgentEvidence = Annotated[
    AgentLiveEvent | AgentTraceFinished | AgentTraceUnavailable,
    Field(discriminator="kind"),
]

AGENT_EVIDENCE: TypeAdapter[AgentEvidence] = TypeAdapter(AgentEvidence)
AGENT_TRACE_TERMINAL: TypeAdapter[AgentTraceTerminal] = TypeAdapter(AgentTraceTerminal)


def terminal_runtime_kind(terminal: AgentTraceFinished | AgentTraceUnavailable) -> RuntimeKind:
    match terminal:
        case AgentTraceFinished(record=record):
            return record.header.runtime_kind
        case AgentTraceUnavailable(runtime_kind=runtime_kind):
            return runtime_kind
