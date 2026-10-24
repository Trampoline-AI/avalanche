"""PredictRLM's native live-event payloads, format ``predict_rlm.run/v1``.

Each live event carries the common event, when there is one, plus the details
only PredictRLM has. These models mirror the parts of PredictRLM's own records
that the common layer does not hold, and import nothing from PredictRLM, so the
operator, client and TUI validate them without the runtime installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, NonNegativeFloat, NonNegativeInt

from ..kinds import CommonEventKind, RuntimeKind

_STRICT = ConfigDict(
    strict=True,
    frozen=True,
    extra="forbid",
    json_schema_serialization_defaults_required=True,
)

FORMAT = "predict_rlm.run/v1"


class PredictRLMTokenUsage(BaseModel):
    """PredictRLM's token counts for one model; ``cache_hits`` counts cached calls."""

    model_config = _STRICT

    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost: NonNegativeFloat
    cache_hits: NonNegativeInt


class PredictRLMUsage(BaseModel):
    """Usage of the main (code-writing) model and the sub-model, kept apart."""

    model_config = _STRICT

    main: PredictRLMTokenUsage
    sub: PredictRLMTokenUsage


class PredictRLMSubCall(BaseModel):
    """One sub-model call made by the agent's code through ``predict()``."""

    model_config = _STRICT

    duration_ms: NonNegativeInt
    usage: PredictRLMTokenUsage
    input: dict[str, JsonValue]
    output: dict[str, JsonValue]
    error: str | None
    finish_reason: str | None


class PredictRLMSubCallGroup(BaseModel):
    """Sub-model calls that share one signature, instructions and model."""

    model_config = _STRICT

    signature: str
    instructions: str | None
    model: str
    total_usage: PredictRLMTokenUsage
    calls: tuple[PredictRLMSubCall, ...]


class PredictRLMOmissions(BaseModel):
    """Calls left out of a turn that exceeded the live event size limit."""

    model_config = _STRICT

    reason: str
    tool_count: NonNegativeInt
    predict_count: NonNegativeInt
    predict_group_count: NonNegativeInt


class _Event(BaseModel):
    model_config = _STRICT

    format: Literal["predict_rlm.run/v1"] = FORMAT
    runtime_kind: ClassVar[RuntimeKind] = "predict_rlm"
    # The common event kind each native kind travels with; ``None`` for none.
    common_kinds: ClassVar[Mapping[str, CommonEventKind | None]]

    @property
    def turn(self) -> int | None:
        """The turn the event belongs to when the common event does not say."""
        return None

    @property
    def error(self) -> str | None:
        """An error the event reports that the common event does not carry."""
        return None


class RunStarted(_Event):
    kind: Literal["run.started"] = "run.started"
    common_kinds = MappingProxyType({"run.started": None})

    input_fields: tuple[str, ...]
    inputs: JsonValue


class IterationRecorded(_Event):
    """PredictRLM's details of a finished turn; the turn itself is the common event."""

    kind: Literal["iteration.recorded"] = "iteration.recorded"
    common_kinds = MappingProxyType({"iteration.recorded": "turn.finished"})

    iteration: NonNegativeInt
    untruncated_output: str
    finish_reason: str | None
    usage: PredictRLMUsage
    predict_calls: tuple[PredictRLMSubCallGroup, ...]
    tool_count: NonNegativeInt
    predict_count: NonNegativeInt
    omissions: PredictRLMOmissions | None


class PredictStarted(_Event):
    kind: Literal["predict.started"] = "predict.started"
    common_kinds = MappingProxyType({"predict.started": "model.started"})

    signature: str
    instructions: str | None


class Correlated(_Event):
    """Native kinds whose common event already carries everything."""

    kind: Literal["predict.finished", "tool.started", "tool.finished", "code.generated"]
    common_kinds = MappingProxyType(
        {
            "predict.finished": "model.finished",
            "tool.started": "tool.started",
            "tool.finished": "tool.finished",
            "code.generated": "code.generated",
        }
    )


class CodeExecuted(_Event):
    kind: Literal["code.executed"] = "code.executed"
    common_kinds = MappingProxyType({"code.executed": None})

    iteration: NonNegativeInt
    output: str | None
    execution_error: str | None

    @property
    def turn(self) -> int | None:
        return self.iteration

    @property
    def error(self) -> str | None:
        return self.execution_error


class RunSucceeded(_Event):
    kind: Literal["run.succeeded"] = "run.succeeded"
    common_kinds = MappingProxyType({"run.succeeded": None})

    status: str
    outputs: JsonValue


class RunEnded(_Event):
    """A run that failed or was cancelled."""

    kind: Literal["run.failed", "run.cancelled"]
    common_kinds = MappingProxyType({"run.failed": None, "run.cancelled": None})

    error_type: str
    run_error: str

    @property
    def error(self) -> str | None:
        return self.run_error


LifecycleKind = Literal[
    "input.prepared",
    "session.started",
    "packages.installed",
    "artifact.mounted",
    "output.reserved",
    "artifact.collected",
    "output.materialized",
    "session.finalized",
    "session.finalize_failed",
    "session.released",
    "session.release_failed",
]


class Lifecycle(_Event):
    """Sandbox and artifact lifecycle steps; Avalanche keeps only that they happened."""

    kind: LifecycleKind
    common_kinds = MappingProxyType(dict.fromkeys(get_args(LifecycleKind)))


PredictRLMNativeEvent = Annotated[
    RunStarted
    | Lifecycle
    | IterationRecorded
    | PredictStarted
    | Correlated
    | CodeExecuted
    | RunSucceeded
    | RunEnded,
    Field(discriminator="kind"),
]
