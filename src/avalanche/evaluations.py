"""Observe completed step values with ordinary selectors and TypeSafe questions."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

from .classifier.classifier_step import Classifier, _close_classifier, _error_description
from .classifier.models import (
    Answer,
    ClassificationResult,
    ClassificationUsage,
    ClassifierDeclaration,
    ClassifierRuntime,
    JSONContent,
    NonemptyString,
    Questions,
    validate_questions,
    validate_runtime_defaults,
    validate_state,
)

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class EvaluationError(RuntimeError):
    """A declaration, selector, Jev request, or composite could not be evaluated."""


@dataclass(frozen=True)
class EvalContext(Generic[InputT, OutputT]):
    """Existing step objects; trace contains serialized terminal invocation events."""

    inputs: Mapping[str, InputT]
    output: OutputT
    trace: list[JsonValue] = field(default_factory=list)


def _require_sync(function: object, description: str) -> None:
    if not callable(function) or inspect.iscoroutinefunction(function):
        raise EvaluationError(f"{description} must be a synchronous callable")
    if inspect.iscoroutinefunction(getattr(function, "__call__", None)):
        raise EvaluationError(f"{description} must be a synchronous callable")


@dataclass(frozen=True, init=False)
class Metric(Generic[InputT, OutputT]):
    """One synchronous state selector and one existing classifier question."""

    state: Callable[[EvalContext[InputT, OutputT]], JSONContent]
    _question_json: str

    def __init__(
        self,
        *,
        state: Callable[[EvalContext[InputT, OutputT]], JSONContent],
        question: dict[str, JsonValue],
    ) -> None:
        _require_sync(state, "Metric state selector")
        try:
            validated = validate_questions({"metric": question})["metric"]
            question_json = validated.model_dump_json()
        except Exception as error:
            raise EvaluationError(
                f"Invalid metric question: {type(error).__name__}; "
                "use one classifier Noul, Score, or Choice question"
            ) from None
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "_question_json", question_json)

    @property
    def question(self) -> dict[str, JsonValue]:
        """Return an owned copy of the validated declaration."""
        return json.loads(self._question_json)


class EvaluationDeclaration(BaseModel):
    """Safe, owned rendering metadata; selectors and composites remain executable code."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    metrics: Questions
    composites: tuple[NonemptyString, ...]
    runtime: ClassifierRuntime

    @field_validator("composites")
    @classmethod
    def unique_composites(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("composite names must be unique")
        return value


class EvaluationResult(BaseModel):
    """Raw TypeSafe answers and explicitly normalized, author-defined composites."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    classification: ClassificationResult
    composites: dict[str, float]


@dataclass(frozen=True, init=False)
class Evaluations(Generic[InputT, OutputT]):
    """Reusable evaluation declaration, independent of workflow scheduling."""

    _metrics: tuple[tuple[str, Metric[InputT, OutputT]], ...]
    _composites: tuple[tuple[str, Callable[[ClassificationResult], float]], ...]
    _runtime_overrides: ClassifierRuntime

    def __init__(
        self,
        *,
        metrics: Mapping[str, Metric[InputT, OutputT]],
        composites: Mapping[str, Callable[[ClassificationResult], float]] | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if not isinstance(metrics, Mapping) or not metrics:
            raise EvaluationError("Evaluations metrics must be a nonempty mapping")
        owned_metrics = tuple(metrics.items())
        for name, metric in owned_metrics:
            if not isinstance(name, str) or not name:
                raise EvaluationError("Metric names must be nonempty strings")
            if not isinstance(metric, Metric):
                raise EvaluationError(f"Metric {name!r} must be a Metric declaration")
        if composites is not None and not isinstance(composites, Mapping):
            raise EvaluationError("Evaluations composites must be a mapping")
        owned_composites = tuple((composites or {}).items())
        for name, function in owned_composites:
            if not isinstance(name, str) or not name:
                raise EvaluationError("Composite names must be nonempty strings")
            _require_sync(function, f"Composite {name!r}")
        overrides: dict[str, JsonValue] = {}
        if model is not None:
            overrides["model"] = model
        if timeout is not None:
            overrides["timeout"] = timeout
        try:
            runtime = ClassifierRuntime.model_validate(validate_runtime_defaults(overrides))
        except Exception as error:
            raise EvaluationError(
                f"Invalid evaluation runtime declaration: {type(error).__name__}; "
                "use classifier model and positive finite timeout settings"
            ) from None
        object.__setattr__(self, "_metrics", owned_metrics)
        object.__setattr__(self, "_composites", owned_composites)
        object.__setattr__(self, "_runtime_overrides", runtime)

    def declaration_metadata(
        self, runtime_defaults: Mapping[str, JsonValue] | None = None
    ) -> EvaluationDeclaration:
        """Describe effective questions and settings without selecting or judging evidence."""
        return EvaluationDeclaration(
            metrics=validate_questions(
                {name: metric.question for name, metric in self._metrics}
            ),
            composites=tuple(name for name, _ in self._composites),
            runtime=self._runtime(runtime_defaults),
        )

    def _runtime(self, runtime_defaults: Mapping[str, JsonValue] | None) -> ClassifierRuntime:
        try:
            return ClassifierRuntime.model_validate(
                {
                    **validate_runtime_defaults(dict(runtime_defaults or {})),
                    **self._runtime_overrides.model_dump(mode="json", exclude_unset=True),
                }
            )
        except Exception as error:
            raise EvaluationError(
                f"Evaluation runtime defaults failed: {type(error).__name__}; "
                "check classifier model and timeout settings"
            ) from None

    async def evaluate(
        self,
        context: EvalContext[InputT, OutputT],
        *,
        runtime_defaults: Mapping[str, JsonValue] | None = None,
    ) -> EvaluationResult:
        """Select evidence, batch equivalent states, then compute ordinary composites."""
        runtime = self._runtime(runtime_defaults)

        batches: dict[str, tuple[JSONContent, dict[str, JsonValue]]] = {}
        for name, metric in self._metrics:
            try:
                selected = metric.state(context)
            except Exception as error:
                raise EvaluationError(
                    f"State selector for metric {name!r} failed: {type(error).__name__}; "
                    "check the selected input, output, or trace field"
                ) from None
            try:
                if inspect.iscoroutine(selected):
                    selected.close()
                state = validate_state(selected)
                # JSON encoding distinguishes true/1 and nested equivalents; sorting
                # object keys groups identical evidence without merging different states.
                key = json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except Exception as error:
                raise EvaluationError(
                    f"State validation for metric {name!r} failed: {type(error).__name__}; "
                    "select text or finite JSON, converting Python-only values explicitly"
                ) from None
            if key not in batches:
                batches[key] = (state, {})
            batches[key][1][name] = metric.question

        classifier = Classifier(
            declaration=ClassifierDeclaration(questions=None, runtime=runtime),
            step_name="evaluations",
        )
        answers: dict[str, Answer] = {}
        resolved_model: str | None = None
        input_tokens: int | None = 0
        output_tokens: int | None = 0
        primary_error: BaseException | None = None
        try:
            for state, questions in batches.values():
                names = ", ".join(repr(name) for name in questions)
                try:
                    result = await classifier(state=state, questions=questions)
                except Exception as error:
                    raise EvaluationError(
                        f"Jev evaluation for metrics {names} failed: "
                        f"{_error_description(error)}"
                    ) from None
                if resolved_model is not None and result.model != resolved_model:
                    raise EvaluationError(
                        f"Jev evaluation for metrics {names} returned a different model; "
                        "pin a model version to combine answers from multiple requests"
                    )
                resolved_model = result.model
                answers.update(result.answers)
                input_tokens = (
                    input_tokens + result.usage.input_tokens
                    if input_tokens is not None and result.usage.input_tokens is not None
                    else None
                )
                output_tokens = (
                    output_tokens + result.usage.output_tokens
                    if output_tokens is not None and result.usage.output_tokens is not None
                    else None
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await _close_classifier(classifier)
            except BaseException as error:
                description = f"Evaluation client cleanup failed: {type(error).__name__}"
                if primary_error is not None:
                    primary_error.add_note(description)
                elif isinstance(error, Exception):
                    raise EvaluationError(description) from None
                else:
                    raise

        # Every declaration contains at least one metric, and every batch has succeeded.
        assert resolved_model is not None
        classification = ClassificationResult(
            model=resolved_model,
            answers={name: answers[name] for name, _ in self._metrics},
            usage=ClassificationUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )
        composites: dict[str, float] = {}
        for name, function in self._composites:
            try:
                # Frozen models still contain mutable dictionaries. Author code must not
                # overwrite raw evidence or influence the next composite's inputs.
                value = function(classification.model_copy(deep=True))
            except Exception as error:
                raise EvaluationError(
                    f"Composite {name!r} failed: {type(error).__name__}; "
                    "check metric names and use the TypeSafe answer accessors"
                ) from None
            if inspect.iscoroutine(value):
                value.close()
            if (
                type(value) not in (int, float)
                or not 0 <= value <= 1
                or not math.isfinite(value)
            ):
                raise EvaluationError(
                    f"Composite {name!r} must return a finite number in [0, 1]; "
                    "normalize an N-level Score with score / (N - 1)"
                )
            composites[name] = float(value)
        return EvaluationResult(classification=classification, composites=composites)
