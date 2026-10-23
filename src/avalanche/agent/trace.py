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

from ..runtime.context import File
from ..workspace import Workspace

_STRICT = ConfigDict(strict=True, frozen=True, extra="forbid")

RuntimeKind = Literal["predict_rlm"]
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


class ToolCallAction(BaseModel):
    """A tool invoked by the model directly or by code the agent ran."""

    model_config = _STRICT

    kind: Literal["tool_call"] = "tool_call"
    call_id: str | None
    caller: ActionCaller
    name: str
    arguments: JsonValue
    result: JsonValue
    error: str | None
    duration_ms: NonNegativeInt | None


class CodeExecutionAction(BaseModel):
    """Code the agent wrote and executed during a turn."""

    model_config = _STRICT

    kind: Literal["code_execution"] = "code_execution"
    language: str
    code: str
    output: str
    error: str | None


class ModelCallAction(BaseModel):
    """A nested model call made during a turn, for example a sub-model query."""

    model_config = _STRICT

    kind: Literal["model_call"] = "model_call"
    call_id: str | None
    caller: ActionCaller
    model: str
    duration_ms: NonNegativeInt | None
    usage: TokenUsage | None
    error: str | None


AgentAction = Annotated[
    ToolCallAction | CodeExecutionAction | ModelCallAction, Field(discriminator="kind")
]


class Turn(BaseModel):
    """One model response and the actions that followed it."""

    model_config = _STRICT

    index: PositiveInt
    text: str | None
    reasoning: str | None
    actions: tuple[AgentAction, ...]
    duration_ms: NonNegativeInt | None
    usage: TokenUsage | None


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
    cache_hits: NonNegativeInt


# A discriminated union on ``format`` once a second runtime adds its format.
NativeTrace = PredictRLMNativeTrace


class AgentTraceRecord(BaseModel):
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
        tool_calls = sum(
            isinstance(action, ToolCallAction) for turn in self.turns for action in turn.actions
        )
        if header.tool_call_count != tool_calls:
            raise ValueError(
                f"header tool_call_count {header.tool_call_count} does not match "
                f"{tool_calls} tool calls in turns"
            )
        return self


class FileInputEvidence(BaseModel):
    """An ``ava.File`` input as recorded in a trace: identity, never bytes."""

    model_config = _STRICT

    kind: Literal["file"] = "file"
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
        if value.sha256 is None:
            raise ValueError("ava.File has no sha256 digest")
        return FileInputEvidence(
            name=value.name,
            content_type=value.content_type,
            size_bytes=len(value.content),
            sha256=value.sha256,
        )
    if value.reference is not None:
        return WorkspaceReferenceInputEvidence(reference=value.reference)
    return WorkspaceInputEvidence(
        entry_count=len(value.entries),
        total_size_bytes=sum(
            len(entry.content) for entry in value.entries if entry.content is not None
        ),
    )
