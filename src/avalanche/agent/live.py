"""Agent evidence on the wire: live events and the one terminal trace per invocation.

While an agent runs, every event it reports crosses to the operator as an
``AgentLiveEvent``: Avalanche's sequence number and timestamp, the common event,
the runtime's native payload, or both. The invocation then ends with one
``AgentTraceFinished`` (the trace header and the step's outputs) or
``AgentTraceUnavailable``. Turns travel only as ``turn.finished`` events.

Nothing here imports DSPy or a runtime SDK, so the operator, client and TUI
validate every body without the runtime extras installed.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
    model_validator,
)

from .kinds import RuntimeKind
from .native import NativeEvent
from .trace import AgentTraceHeader, AgentTraceStatus, Turn

# Serialized bodies always carry defaulted fields such as ``kind``; their
# exported JSON Schema says so.
_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)


class RunStarted(BaseModel):
    """The call as the runtime started it: the step's inputs and the model it asked for.

    Files and workspaces appear by reference. Inputs too large for the event are
    left out and named in ``omitted_inputs``.
    """

    model_config = _STRICT

    kind: Literal["run.started"] = "run.started"
    model: str | None
    inputs: dict[str, JsonValue]
    omitted_inputs: tuple[str, ...]


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
    # The turn that called the tool, when the runtime knows it.
    turn: PositiveInt | None


class ToolFinished(BaseModel):
    model_config = _STRICT

    kind: Literal["tool.finished"] = "tool.finished"
    call_id: str
    name: str
    turn: PositiveInt | None
    error: str | None


AgentEvent = Annotated[
    RunStarted | TurnFinished | ToolStarted | ToolFinished,
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
    common: AgentEvent | None
    native: NativeEvent | None

    @model_validator(mode="after")
    def _check_parts(self) -> Self:
        native = self.native
        if native is None:
            if self.common is None:
                raise ValueError("a live event needs a common event, a native event or both")
            return self
        if isinstance(self.common, TurnFinished) and self.common.turn.native.format != (
            native.format
        ):
            raise ValueError(
                f"turn of format {self.common.turn.native.format!r} does not belong to "
                f"native format {native.format!r}"
            )
        return self

    @property
    def runtime_kind(self) -> RuntimeKind | None:
        """The runtime that sent the event, when it has a native part to say so."""
        return self.native.runtime_kind if self.native is not None else None

    @property
    def kind_label(self) -> str:
        """The native kind, else the common one: what the event is called in summaries."""
        if self.native is not None:
            return self.native.kind
        assert self.common is not None
        return self.common.kind

    @property
    def turn(self) -> int | None:
        match self.common:
            case TurnFinished(turn=turn):
                return turn.index
            case ToolStarted(turn=turn) | ToolFinished(turn=turn) if turn is not None:
                return turn
        return self.native.turn if self.native is not None else None

    @property
    def error(self) -> str | None:
        match self.common:
            case ToolFinished(error=error) if error is not None:
                return error
        return self.native.error if self.native is not None else None

    @property
    def failed(self) -> bool:
        """Whether the event reports a failure, with or without an error text.

        A turn fails as its runtime decides (failed code, an API error); the agent
        may have handled a failed tool call inside it.
        """
        failed_turn = isinstance(self.common, TurnFinished) and self.common.turn.failed
        return self.error is not None or failed_turn


class AgentTraceFinished(BaseModel):
    """The end of an invocation: its trace header and the step's outputs.

    The turns travelled as live events. ``outputs`` is ``None`` when the run
    returned none; files and workspaces appear by reference, and fields too large
    to keep are left out and named in ``omitted_outputs``. ``complete`` says
    whether every live event of the invocation was delivered.
    """

    model_config = _STRICT

    kind: Literal["trace_finished"] = "trace_finished"
    schema_version: Literal[2] = 2
    invocation_id: str = Field(min_length=1)
    header: AgentTraceHeader
    outputs: dict[str, JsonValue] | None
    omitted_outputs: tuple[str, ...]
    complete: bool

    @model_validator(mode="after")
    def _check_outputs(self) -> Self:
        if self.outputs is None and self.omitted_outputs:
            raise ValueError("a run without outputs has none to omit")
        if self.outputs is not None and set(self.outputs) & set(self.omitted_outputs):
            raise ValueError("an omitted output must not also be kept")
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
        case AgentTraceFinished(header=header):
            return header.runtime_kind
        case AgentTraceUnavailable(runtime_kind=runtime_kind):
            return runtime_kind
