"""Process-local handoff of successful step evaluations to an operator collector."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from pydantic import JsonValue

from ._agent_evidence import AgentEvidenceObserverEvent, capture_agent_evidence
from .evaluations import EvalContext, Evaluations

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class EvaluationSubmission:
    """Live execution objects; the collector must snapshot them before returning."""

    evaluations: Evaluations[object, object]
    context: EvalContext[object, object] | None
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
_EVALUATION_INJECTED_PARAMS: ContextVar[frozenset[str]] = ContextVar(
    "avalanche_evaluation_injected_params", default=frozenset()
)


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


class _StepEvaluationCapture(Generic[InputT, OutputT]):
    def __init__(
        self,
        evaluations: Evaluations[InputT, OutputT],
        step_name: str,
    ) -> None:
        self.evaluations = evaluations
        self.step_name = step_name
        self.collector = _EVALUATION_COLLECTOR.get()
        self.inputs: dict[str, object] = {}
        self.trace: list[JsonValue] = []
        self.error: str | None = None

    def submit(self, output: object) -> None:
        if self.collector is None:
            _diagnostic(
                f"Evaluations for agent step {self.step_name!r} were not evaluated: "
                "embedded execution has no operator evaluation collector."
            )
            return

        from .classifier.classifier_step import _WORKFLOW_CLASSIFIER_DEFAULTS

        context = None
        runtime_defaults: Mapping[str, JsonValue] = {}
        try:
            runtime_defaults = _WORKFLOW_CLASSIFIER_DEFAULTS.get()
            if self.error is None:
                context = EvalContext(inputs=self.inputs, output=output, trace=self.trace)
        except BaseException as exc:
            self.error = _error_message(exc)
        submission = EvaluationSubmission(
            evaluations=cast(Evaluations[object, object], self.evaluations),
            context=context,
            runtime_defaults=runtime_defaults,
            error=self.error,
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


@contextmanager
def capture_step_evaluations(
    evaluations: Evaluations[InputT, OutputT],
    *,
    step_name: str,
    signature: inspect.Signature,
    input_names: frozenset[str],
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> Iterator[_StepEvaluationCapture[InputT, OutputT]]:
    """Collect terminal events while preserving the existing observer's policy."""
    from ._agent_evidence import _AGENT_EVIDENCE_OBSERVER

    capture = _StepEvaluationCapture(evaluations, step_name)
    if capture.collector is None:
        yield capture
        return

    try:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        excluded = _EVALUATION_INJECTED_PARAMS.get()
        capture.inputs = {
            name: value
            for name, value in bound.arguments.items()
            if name in input_names and name not in excluded
        }
    except BaseException as exc:
        capture.error = _error_message(exc)

    previous = _AGENT_EVIDENCE_OBSERVER.get()

    def observe(event: AgentEvidenceObserverEvent) -> None:
        try:
            if event["kind"] in {"trace_finished", "trace_unavailable"}:
                # Terminal evidence already uses the existing JSON export boundary.
                capture.trace.append(cast(JsonValue, event))
        except BaseException as exc:
            capture.error = _error_message(exc)
        if previous is not None:
            listener, errors = previous
            if errors == "raise":
                listener(event)
            else:
                try:
                    listener(event)
                except Exception:
                    pass

    with capture_agent_evidence(observe, errors="raise"):
        yield capture


__all__ = [
    "EvaluationCaptureError",
    "EvaluationSubmission",
    "EvaluationSubmitCallback",
    "capture_evaluations",
]
