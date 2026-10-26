"""PredictRLM's native turn part and live-event payloads, format ``predict_rlm.run/v1``.

Each turn carries PredictRLM's own part (its code and output), and each live
event carries the common event, when there is one, plus the details only
PredictRLM has. These models mirror the parts of PredictRLM's own records
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


class PredictRLMPredictCall(BaseModel):
    """One sub-model call made by the agent's code through ``predict()``.

    Its inputs, outputs, error and duration are the matching ``predict`` tool call.
    """

    model_config = _STRICT

    signature: str
    instructions: str | None
    model: str
    usage: PredictRLMTokenUsage
    finish_reason: str | None


class PredictRLMTurn(BaseModel):
    """PredictRLM's own details of one turn (iteration).

    ``predict_calls`` describe the turn's last ``len(predict_calls)`` tool calls,
    which are its ``predict`` calls, in the same order.
    """

    model_config = _STRICT

    format: Literal["predict_rlm.run/v1"] = FORMAT
    code: str
    # The code's output as shown to the model, and in full.
    output: str
    untruncated_output: str
    # The code failed.
    error: bool
    finish_reason: str | None
    usage: PredictRLMUsage
    predict_calls: tuple[PredictRLMPredictCall, ...]

    @property
    def failed(self) -> bool:
        return self.error


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
    """A finished turn, which is the common event; this says what its live event left out."""

    kind: Literal["iteration.recorded"] = "iteration.recorded"
    common_kinds = MappingProxyType({"iteration.recorded": "turn.finished"})

    omissions: PredictRLMOmissions | None


class CodeGenerated(_Event):
    """The code of a turn still running; the finished turn carries it too."""

    kind: Literal["code.generated"] = "code.generated"
    common_kinds = MappingProxyType({"code.generated": None})

    iteration: NonNegativeInt
    code: str

    @property
    def turn(self) -> int | None:
        return self.iteration


class PredictStarted(_Event):
    """A sub-model call started by the agent's code."""

    kind: Literal["predict.started"] = "predict.started"
    common_kinds = MappingProxyType({"predict.started": None})

    call_id: str
    signature: str
    instructions: str | None
    model: str


class PredictFinished(_Event):
    kind: Literal["predict.finished"] = "predict.finished"
    common_kinds = MappingProxyType({"predict.finished": None})

    call_id: str
    predict_error: str | None

    @property
    def error(self) -> str | None:
        return self.predict_error


class Correlated(_Event):
    """Native kinds whose common event already carries everything."""

    kind: Literal["tool.started", "tool.finished"]
    common_kinds = MappingProxyType(
        {"tool.started": "tool.started", "tool.finished": "tool.finished"}
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
    | CodeGenerated
    | PredictStarted
    | PredictFinished
    | Correlated
    | CodeExecuted
    | RunSucceeded
    | RunEnded,
    Field(discriminator="kind"),
]
