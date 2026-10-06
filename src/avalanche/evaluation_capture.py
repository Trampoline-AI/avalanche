"""Process-local handoff of first-returned agent evaluations to an operator collector."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar, cast

from dspy import Prediction
from pydantic import JsonValue

from ._agent_evidence import AgentTraceFinishedEvent, AgentTraceUnavailableEvent
from .evaluations import EvalContext, Evaluations

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class EvaluationSubmission:
    """Live execution objects; the collector must snapshot them before returning."""

    evaluations: Evaluations[object, Prediction]
    context: EvalContext[object, Prediction] | None
    runtime_defaults: Mapping[str, JsonValue]
    error: str | None = None


@dataclass(frozen=True)
class EvaluationCaptureError:
    """A collector failure retained for operator reporting, never workflow failure."""

    submission: EvaluationSubmission
    error: str


EvaluationSubmitCallback = Callable[[EvaluationSubmission], None]
_EVALUATION_COLLECTOR: ContextVar[
    tuple[EvaluationSubmitCallback, list[EvaluationCaptureError]] | None
] = ContextVar("avalanche_evaluation_collector", default=None)


@contextmanager
def capture_evaluations(
    submit_callback: EvaluationSubmitCallback,
) -> Iterator[list[EvaluationCaptureError]]:
    """Install a synchronous snapshot callback and expose any failed handoffs.

    No selectors or model calls run here. The callback receives live values only
    for the duration of its call and must own its snapshot before returning.
    """
    errors: list[EvaluationCaptureError] = []
    token = _EVALUATION_COLLECTOR.set((submit_callback, errors))
    try:
        yield errors
    finally:
        _EVALUATION_COLLECTOR.reset(token)


def _error_message(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"
    except BaseException:
        return type(error).__name__


def _diagnostic(message: str) -> None:
    # Custom logging handlers (including warnings-as-errors) must not affect a step.
    try:
        logging.getLogger(__name__).warning(message)
    except BaseException:
        pass


class _StepEvaluationCapture:
    def __init__(
        self,
        evaluations: Evaluations[object, Prediction],
        step_name: str,
    ) -> None:
        self.evaluations = evaluations
        self.step_name = step_name
        self.collector = _EVALUATION_COLLECTOR.get()
        self.claimed = False

    def submit(
        self,
        inputs: Mapping[str, InputT],
        output: Prediction,
        terminal_event: AgentTraceFinishedEvent | AgentTraceUnavailableEvent,
    ) -> None:
        if self.claimed:
            return
        # No await before or during handoff: the first successful return owns
        # this slot, including any evaluation-only snapshot/submission failure.
        self.claimed = True
        if self.collector is None:
            _diagnostic(
                f"Evaluations for agent step {self.step_name!r} were not evaluated: "
                "embedded execution has no operator evaluation collector."
            )
            return

        from .classifier.classifier_step import _WORKFLOW_CLASSIFIER_DEFAULTS

        context: EvalContext[object, Prediction] | None = None
        error = None
        runtime_defaults: Mapping[str, JsonValue] = {}
        try:
            runtime_defaults = _WORKFLOW_CLASSIFIER_DEFAULTS.get()
            if terminal_event["kind"] == "trace_finished":
                trace: list[JsonValue] = [
                    {
                        "kind": terminal_event["kind"],
                        "invocation_id": terminal_event["invocation_id"],
                        # The operator event omits iterations already streamed.
                        # Evaluations retain the full winning invocation instead.
                        "trace": json.loads(output.trace.to_exportable_json()),
                    }
                ]
            else:
                trace = [
                    {
                        "kind": terminal_event["kind"],
                        "invocation_id": terminal_event["invocation_id"],
                        "error": terminal_event["error"],
                    }
                ]
            context = EvalContext(inputs=inputs, output=output, trace=trace)
        except BaseException as exc:
            error = _error_message(exc)
        submission = EvaluationSubmission(
            evaluations=self.evaluations,
            context=context,
            runtime_defaults=runtime_defaults,
            error=error,
        )
        callback, errors = self.collector
        try:
            callback_result = callback(submission)
            if inspect.isawaitable(callback_result):
                if inspect.iscoroutine(callback_result):
                    callback_result.close()
                raise TypeError("evaluation submit callback must be synchronous")
        except BaseException as exc:
            error = _error_message(exc)
            errors.append(EvaluationCaptureError(submission, error))
            _diagnostic(
                f"Evaluation submission for agent step {self.step_name!r} failed: {error}"
            )


_STEP_EVALUATION_CAPTURE: ContextVar[_StepEvaluationCapture | None] = ContextVar(
    "avalanche_step_evaluation_capture", default=None
)


@contextmanager
def capture_step_evaluations(
    evaluations: Evaluations[InputT, OutputT],
    *,
    step_name: str,
) -> Iterator[None]:
    """Install one first-successful-return slot for this step's agent calls."""
    capture = _StepEvaluationCapture(
        cast(Evaluations[object, Prediction], evaluations), step_name
    )
    token = _STEP_EVALUATION_CAPTURE.set(capture)
    try:
        yield
    finally:
        _STEP_EVALUATION_CAPTURE.reset(token)


__all__ = [
    "EvaluationCaptureError",
    "EvaluationSubmission",
    "EvaluationSubmitCallback",
    "capture_evaluations",
]
