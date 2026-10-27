"""Runtime-neutral agent trace models.

Every agent runtime reports the same header and turns, named after the
OpenTelemetry conventions for AI calls, plus a runtime-specific native part
on the header and on each turn. The native unions are closed and defined without
SDK imports, so the operator and TUI can validate every format without runtime
extras.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Annotated, Literal, Self

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
from pydantic_core import to_json

from ..file import File
from ..workspace import Workspace
from .kinds import RuntimeKind
from .native import TraceHeaderNative, TurnNative
from .native.predict_rlm import PredictRLMTurn

# Serialized bodies always carry defaulted fields such as ``kind``; their
# exported JSON Schema says so.
_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)

AgentTraceStatus = Literal["succeeded", "failed", "cancelled"]
# The header, its runtime part included, travels in every run snapshot, so that part
# stays small; anything large belongs in the turns.
TRACE_HEADER_NATIVE_LIMIT_BYTES = 64 * 1024
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

    status: AgentTraceStatus
    stop_reason: str | None
    model: str | None
    duration_ms: NonNegativeInt
    usage: TokenUsage | None
    turn_count: NonNegativeInt
    tool_call_count: NonNegativeInt
    external_run_id: str | None
    session_id: str | None
    native: TraceHeaderNative

    @property
    def runtime_kind(self) -> RuntimeKind:
        return self.native.runtime_kind

    @property
    def trace_format(self) -> str:
        return self.native.format

    @model_validator(mode="after")
    def _check_native(self) -> Self:
        size = len(self.native.model_dump_json().encode())
        if size > TRACE_HEADER_NATIVE_LIMIT_BYTES:
            raise ValueError(
                f"the header's {self.native.format!r} part is {size} bytes; it travels "
                f"in every run snapshot, so the limit is {TRACE_HEADER_NATIVE_LIMIT_BYTES}"
            )
        return self


class AgentTrace(BaseModel):
    """The complete trace of one agent invocation, kept in process.

    On the wire the turns travel as live events and the final event carries the
    header alone.
    """

    model_config = _STRICT

    header: AgentTraceHeader
    turns: tuple[Turn, ...]

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        header = self.header
        indexes = [turn.index for turn in self.turns]
        if indexes != list(range(1, len(self.turns) + 1)):
            raise ValueError(f"turn indexes must run 1..n in order, got {indexes}")
        if header.turn_count != len(self.turns):
            raise ValueError(
                f"header turn_count {header.turn_count} does not match "
                f"{len(self.turns)} turns"
            )
        formats = {turn.native.format for turn in self.turns} - {header.trace_format}
        if formats:
            raise ValueError(
                f"turns of format {sorted(formats)} do not belong to a "
                f"{header.trace_format!r} trace"
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


# Inputs and outputs a trace keeps, all fields together; the largest fields are left
# out beyond this, so one event stays well under the operator's body limits.
RETAINED_VALUES_LIMIT_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_COLLECTION_ITEMS = 10_000
_MAX_EVIDENCE_DEPTH = 32


def unavailable_value(reason: str) -> dict[str, JsonValue]:
    """The marker that stands in for a value a trace cannot keep."""
    return {"kind": "unavailable", "reason": reason}


def evidence_value(value: object, *, depth: int = 0) -> JsonValue:
    """A step value as a trace keeps it: JSON, with files and workspaces by reference."""
    if depth > _MAX_EVIDENCE_DEPTH:
        return unavailable_value("maximum nesting depth exceeded")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else unavailable_value("non-finite number")
    if isinstance(value, File | Workspace):
        return file_input_evidence(value).model_dump(mode="json")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return unavailable_value("mapping exceeds item limit")
        projected: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return unavailable_value("mapping keys must be strings")
            projected[key] = evidence_value(item, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_EVIDENCE_COLLECTION_ITEMS:
            return unavailable_value("sequence exceeds item limit")
        return [evidence_value(item, depth=depth + 1) for item in value]
    return unavailable_value(f"unsupported value type: {type(value).__name__}")


def retained_fields(
    values: Mapping[str, object],
) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
    """Fields as a trace keeps them, and the names of those left out for size."""
    projected = {name: evidence_value(value) for name, value in values.items()}
    sizes = {name: len(to_json(value)) for name, value in projected.items()}
    total = len(to_json(projected))
    omitted: set[str] = set()
    for name in sorted(projected, key=sizes.__getitem__, reverse=True):
        if total <= RETAINED_VALUES_LIMIT_BYTES:
            break
        total -= sizes[name]
        omitted.add(name)
    return (
        {name: value for name, value in projected.items() if name not in omitted},
        tuple(name for name in projected if name in omitted),
    )
