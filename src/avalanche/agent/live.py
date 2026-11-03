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
from pydantic_core import to_json

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


# Each text of a prompt is kept up to this many characters, and each tool's schema up
# to this many bytes of JSON. Every agent call sends a prompt event, and the operator
# counts it against each node's and run's detail budget; the step's inputs, which the
# first message repeats, are kept whole in ``run.started``.
PROMPT_TEXT_LIMIT_CHARS = 64 * 1024
PROMPT_SCHEMA_LIMIT_BYTES = 64 * 1024


class PromptText(BaseModel):
    """One text of a prompt as a trace keeps it; text past the limit is left out."""

    model_config = _STRICT

    text: str = Field(max_length=PROMPT_TEXT_LIMIT_CHARS)
    # Characters left out at the end of the text because it is too large to keep.
    omitted_chars: NonNegativeInt

    @classmethod
    def of(cls, text: str) -> Self:
        return cls(
            text=text[:PROMPT_TEXT_LIMIT_CHARS],
            omitted_chars=max(0, len(text) - PROMPT_TEXT_LIMIT_CHARS),
        )


def prompt_schema(schema: JsonValue) -> JsonValue:
    """A schema as a prompt event keeps it, or a marker when it is too large to keep."""
    if len(to_json(schema)) > PROMPT_SCHEMA_LIMIT_BYTES:
        return {"kind": "unavailable", "reason": "schema too large to keep in the trace"}
    return schema


class PromptTextPart(BaseModel):
    model_config = _STRICT

    kind: Literal["text"] = "text"
    text: PromptText


class PromptFilePart(BaseModel):
    """A file sent with a message; the model reads its contents, the trace never keeps them."""

    model_config = _STRICT

    kind: Literal["file"] = "file"
    name: str | None
    content_type: str | None


PromptPart = Annotated[PromptTextPart | PromptFilePart, Field(discriminator="kind")]


class PromptMessage(BaseModel):
    model_config = _STRICT

    role: str
    parts: tuple[PromptPart, ...]


class PromptTool(BaseModel):
    """A tool as the model is shown it: its name, description and input schema."""

    model_config = _STRICT

    name: str
    description: PromptText | None
    # The JSON Schema of the tool's input, or the runtime's own definition of a tool
    # that takes free text, such as a grammar.
    input_schema: JsonValue


class PromptSent(BaseModel):
    """Everything the model is given before its first turn.

    The runtime reads it from its engine's own record of the first model request: the
    system prompt, including any text the engine adds, the messages, every tool, and the
    answer's schema when it is not a tool. Files appear by name; their contents are never
    kept. The text is kept as the model got it, so it can name input files by their path
    on the machine that ran the step.
    """

    model_config = _STRICT

    kind: Literal["prompt.sent"] = "prompt.sent"
    # The system prompt's blocks in order; empty when there is none.
    system: tuple[PromptText, ...]
    messages: tuple[PromptMessage, ...]
    tools: tuple[PromptTool, ...]
    # The answer's JSON Schema when the model is given it apart from the tools.
    output_schema: JsonValue | None


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
    RunStarted | PromptSent | TurnFinished | ToolStarted | ToolFinished,
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
        """The runtime that sent the event, when a native part, its own or its turn's, says."""
        if self.native is not None:
            return self.native.runtime_kind
        if isinstance(self.common, TurnFinished):
            return self.common.turn.native.runtime_kind
        return None

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
