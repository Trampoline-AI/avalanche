"""Runtime-neutral agent trace models.

Every agent runtime reports the same header and turns, named after the
OpenTelemetry conventions for AI calls, plus one runtime-specific native
section. The native union is closed and defined here without SDK imports, so
the operator and TUI can validate every format without runtime extras.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    model_validator,
)

from ..file import File
from ..workspace import Workspace
from .kinds import RuntimeKind
from .native import TurnNative
from .native.predict_rlm import PredictRLMTurn, PredictRLMUsage

# Serialized bodies always carry defaulted fields such as ``kind``; their
# exported JSON Schema says so.
_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)

AgentTraceStatus = Literal["succeeded", "failed", "cancelled"]
ActionCaller = Literal["model", "code"]


class TokenUsage(BaseModel):
    """Token counts for one or more model calls; absent figures were not reported."""

    model_config = _STRICT

    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt | None = None
    cache_creation_input_tokens: NonNegativeInt | None = None
    cost_usd: NonNegativeFloat | None = None


class ToolCall(BaseModel):
    """A tool invoked by the model directly or by code the agent ran."""

    model_config = _STRICT

    call_id: str | None
    caller: ActionCaller
    name: str
    arguments: JsonValue
    result: JsonValue
    error: str | None
    duration_ms: NonNegativeInt | None


class Turn(BaseModel):
    """One model response and the tool calls that followed it."""

    model_config = _STRICT

    index: PositiveInt
    # What the model wrote this turn, as a reader would see it.
    message: str | None
    # The model provider's own reasoning (thinking tokens), only when the runtime
    # exposes it; never the turn's message.
    reasoning: str | None
    tool_calls: tuple[ToolCall, ...]
    duration_ms: NonNegativeInt | None
    # The whole turn, every model it called summed.
    usage: TokenUsage | None
    native: TurnNative

    @model_validator(mode="after")
    def _check_native(self) -> Self:
        if isinstance(self.native, PredictRLMTurn):
            predict_calls = len(self.native.predict_calls)
            tail = self.tool_calls[len(self.tool_calls) - predict_calls :]
            if len(tail) != predict_calls or any(
                call.name != "predict" or call.caller != "code" for call in tail
            ):
                raise ValueError(
                    f"a PredictRLM turn's last {predict_calls} tool calls must be its "
                    "predict calls"
                )
        return self

    @property
    def failed(self) -> bool:
        """Whether the turn failed as its runtime decides: failed code or an API error."""
        return self.native.failed


class AgentTraceHeader(BaseModel):
    """Summary every runtime reports for one agent invocation."""

    model_config = _STRICT

    runtime_kind: RuntimeKind
    trace_format: str
    status: AgentTraceStatus
    stop_reason: str | None
    model: str | None
    duration_ms: NonNegativeInt
    usage: TokenUsage | None
    turn_count: NonNegativeInt
    tool_call_count: NonNegativeInt
    external_run_id: str | None
    session_id: str | None


class PredictRLMNativeTrace(BaseModel):
    """PredictRLM run details that have no common-layer equivalent."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "predict_rlm"

    format: Literal["predict_rlm.run/v1"] = "predict_rlm.run/v1"
    sub_model: str | None
    max_iterations: PositiveInt
    usage: PredictRLMUsage
    # PredictRLM's compact reference for joining a partial trace to its events.
    telemetry_ref: dict[str, JsonValue] | None


class ClaudeAgentNativeTrace(BaseModel):
    """Claude Agent SDK session details that have no common-layer equivalent."""

    model_config = _STRICT

    runtime_kind: ClassVar[RuntimeKind] = "claude_agent"

    format: Literal["claude_agent.messages/v1"] = "claude_agent.messages/v1"
    # The SDK's result subtype, such as ``success`` or ``error_max_turns``.
    result_subtype: str | None
    # Turns as the CLI counts them, which include tool-result round trips.
    num_turns: NonNegativeInt
    duration_api_ms: NonNegativeInt | None
    permission_denials: NonNegativeInt
    errors: tuple[str, ...]


NativeTrace = Annotated[
    PredictRLMNativeTrace | ClaudeAgentNativeTrace, Field(discriminator="format")
]


class AgentTrace(BaseModel):
    """The complete trace of one agent invocation.

    When ``turns_streamed`` is true, turns travelled as live events and the
    record carries none; the header still states how many there were.
    """

    model_config = _STRICT

    schema_version: Literal[2] = 2
    header: AgentTraceHeader
    turns: tuple[Turn, ...]
    native: NativeTrace
    turns_streamed: bool

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        header = self.header
        if header.trace_format != self.native.format:
            raise ValueError(
                f"header trace_format {header.trace_format!r} does not match "
                f"native format {self.native.format!r}"
            )
        if header.runtime_kind != self.native.runtime_kind:
            raise ValueError(
                f"header runtime_kind {header.runtime_kind!r} does not match "
                f"native format {self.native.format!r}"
            )
        if self.turns_streamed:
            if self.turns:
                raise ValueError("a trace with streamed turns must not carry turns")
            return self
        indexes = [turn.index for turn in self.turns]
        if indexes != list(range(1, len(self.turns) + 1)):
            raise ValueError(f"turn indexes must run 1..n in order, got {indexes}")
        if header.turn_count != len(self.turns):
            raise ValueError(
                f"header turn_count {header.turn_count} does not match "
                f"{len(self.turns)} turns"
            )
        formats = {turn.native.format for turn in self.turns} - {self.native.format}
        if formats:
            raise ValueError(
                f"turns of format {sorted(formats)} do not belong to a "
                f"{self.native.format!r} trace"
            )
        tool_calls = sum(len(turn.tool_calls) for turn in self.turns)
        if header.tool_call_count != tool_calls:
            raise ValueError(
                f"header tool_call_count {header.tool_call_count} does not match "
                f"{tool_calls} tool calls in turns"
            )
        return self


class FileInputEvidence(BaseModel):
    """An ``ava.File`` input in a trace: its reference, never bytes or host paths."""

    model_config = _STRICT

    kind: Literal["file"] = "file"
    workspace: str
    path: str
    name: str | None
    content_type: str | None
    size_bytes: NonNegativeInt
    sha256: str


class WorkspaceInputEvidence(BaseModel):
    """A value ``ava.Workspace`` input as recorded in a trace."""

    model_config = _STRICT

    kind: Literal["workspace"] = "workspace"
    entry_count: NonNegativeInt
    total_size_bytes: NonNegativeInt


class WorkspaceReferenceInputEvidence(BaseModel):
    """A reference ``ava.Workspace`` input: its opaque reference, never the host path."""

    model_config = _STRICT

    kind: Literal["workspace_reference"] = "workspace_reference"
    reference: str


FileInputEvidenceValue = Annotated[
    FileInputEvidence | WorkspaceInputEvidence | WorkspaceReferenceInputEvidence,
    Field(discriminator="kind"),
]


def file_input_evidence(value: File | Workspace) -> FileInputEvidenceValue:
    """Describe a file or workspace input without its paths or bytes."""
    if isinstance(value, File):
        # The wire form streams a bound file's digest and requires one when unbound.
        identity = File.model_validate(value.model_dump())
        return FileInputEvidence(
            workspace=identity.ref.workspace,
            path=identity.ref.path,
            name=identity.name,
            content_type=identity.content_type,
            size_bytes=identity.size,
            sha256=identity.sha256,
        )
    if value.reference is not None:
        return WorkspaceReferenceInputEvidence(reference=value.reference)
    return WorkspaceInputEvidence(
        entry_count=len(value.entries),
        total_size_bytes=sum(
            len(entry.content) for entry in value.entries if entry.content is not None
        ),
    )
