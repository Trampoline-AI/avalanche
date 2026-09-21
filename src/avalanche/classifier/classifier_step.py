"""Bodyful classifier steps backed by the process-local asynchronous TypeSafe SDK."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import update_wrapper
from types import SimpleNamespace, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, JsonValue, PydanticUserError, TypeAdapter, ValidationError
from pydantic.json_schema import JsonSchemaMode

from ..dag import Node, NodeType, _matching_provider, _safe_issubclass
from .evidence import emit_classifier_evidence
from .models import (
    ClassificationResult,
    ClassifierDeclaration,
    ClassifierInvocation,
    ClassifierRuntime,
    ClassifierStepInput,
    ClassifierStepOutput,
    JSONContent,
    validate_questions,
    validate_runtime_defaults,
    validate_state,
)

if TYPE_CHECKING:
    from typesafe_sdk import AsyncTypeSafeClient, SystemOneResponse


class ClassifierStepError(RuntimeError):
    """An invalid classifier declaration or invocation boundary."""


class ClassifierStepExecutionError(RuntimeError):
    """A TypeSafe invocation or classifier resource cleanup failed."""


_WORKFLOW_CLASSIFIER_DEFAULTS: ContextVar[Mapping[str, JsonValue]] = ContextVar(
    "avalanche_classifier_workflow_defaults", default={}
)


def _error_description(error: BaseException) -> str:
    """Describe failures without leaking request state or server-echoed secrets into errors."""
    from typesafe_sdk import TypeSafeAPIError

    if isinstance(error, (ClassifierStepError, ClassifierStepExecutionError)):
        return str(error)
    if isinstance(error, TypeSafeAPIError):
        return f"{type(error).__name__} (HTTP {error.status})"
    # Validation messages and locations can also contain input values or keys.
    return type(error).__name__


def _build_client(runtime: ClassifierRuntime) -> AsyncTypeSafeClient:
    """Resolve credentials and construct the SDK only inside the executing process."""
    from typesafe_sdk import AsyncTypeSafeClient

    if "TYPESAFE_API_KEY" not in os.environ:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True), override=False)

    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        raise ClassifierStepExecutionError("TYPESAFE_API_KEY is required to call a classifier")
    return AsyncTypeSafeClient(model=runtime.model, timeout=runtime.timeout)


def _adapt_response(
    response: SystemOneResponse, declaration: ClassifierDeclaration
) -> ClassificationResult:
    # The SDK exposes the original HTTP body on its typed response. Validate that
    # boundary directly: SDK decoding intentionally drops unknown answer types and
    # normalizes integer Score keys, which must not conceal malformed responses.
    try:
        result = ClassificationResult.model_validate_json(response.raw_http_response.content)
        result.validate_declaration(declaration)
    except ValueError as error:
        description = _error_description(error)
        raise ClassifierStepExecutionError(
            f"Invalid TypeSafe response: {description}"
        ) from None
    return result


class Classifier:
    """Injected callable retaining each validated request state in lifecycle evidence."""

    def __init__(
        self,
        *,
        declaration: ClassifierDeclaration,
        step_name: str,
        input_model: type[BaseModel] | None = None,
    ) -> None:
        self._declaration = declaration
        self._step_name = step_name
        self._input_model = input_model
        self._client: AsyncTypeSafeClient | None = None
        self._invocation_index = 0
        self._closed = False
        self._pending: set[asyncio.Task[object]] = set()

    async def __call__(self, *, state: JSONContent) -> ClassificationResult:
        if self._closed:
            raise ClassifierStepError("classifier cannot be used after its step has finished")
        invocation_index = self._invocation_index
        self._invocation_index += 1
        started_at = time.time()
        record = ClassifierInvocation(
            invocation_id=uuid.uuid4().hex,
            invocation_index=invocation_index,
            status="running",
            started_at=started_at,
            ended_at=None,
            declaration=self._declaration,
            input=None,
            result=None,
            error=None,
        )
        task = asyncio.current_task()
        if task is not None:
            self._pending.add(task)
        try:
            try:
                try:
                    request_state = validate_state(state)
                    if self._input_model is not None:
                        validated = self._input_model.model_validate_json(
                            json.dumps(request_state, ensure_ascii=False, allow_nan=False)
                        )
                        request_state = validate_state(
                            validated.model_dump(mode="json", by_alias=True)
                        )
                    record = record.model_copy(update={"input": deepcopy(request_state)})
                finally:
                    # Rejected state still gets a running/failed pair with null input.
                    emit_classifier_evidence(record)
                if self._client is None:
                    self._client = _build_client(self._declaration.runtime)
                from typesafe_sdk import Choice, Noul, NoulCriteria, Question, Score

                from .models import ChoiceQuestion, NoulQuestion

                questions: dict[str, Question] = {}
                for name, question in self._declaration.questions.items():
                    if isinstance(question, ChoiceQuestion):
                        questions[name] = Choice(
                            instructions=question.instructions, criteria=question.criteria
                        )
                    elif isinstance(question, NoulQuestion):
                        criteria: NoulCriteria | None = None
                        if question.criteria is not None:
                            criteria = NoulCriteria()
                            if "true" in question.criteria:
                                criteria["true"] = question.criteria["true"]
                            if "false" in question.criteria:
                                criteria["false"] = question.criteria["false"]
                        questions[name] = Noul(
                            instructions=question.instructions, criteria=criteria
                        )
                    else:
                        questions[name] = Score(
                            instructions=question.instructions, criteria=question.criteria
                        )
                response = await self._client.system_one(
                    state=request_state, questions=questions
                )
                result = _adapt_response(response, self._declaration)
            except BaseException as error:
                status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                description = _error_description(error)
                terminal = record.model_copy(
                    update={
                        "status": status,
                        "ended_at": max(started_at, time.time()),
                        "error": description,
                    }
                )
                failure = (
                    ClassifierStepExecutionError(
                        f"classifier step {self._step_name!r} failed: {description}"
                    )
                    if isinstance(error, Exception)
                    else error
                )
                try:
                    emit_classifier_evidence(terminal)
                except BaseException as evidence_error:
                    failure.add_note(
                        "Classifier evidence capture failed: "
                        f"{_error_description(evidence_error)}"
                    )
                if not isinstance(error, Exception):
                    raise
                raise failure from None
            emit_classifier_evidence(
                record.model_copy(
                    update={
                        "status": "success",
                        "ended_at": max(started_at, time.time()),
                        "result": result,
                    }
                )
            )
            return result
        finally:
            if task is not None:
                self._pending.discard(task)

    async def aclose(self) -> None:
        """Finish outstanding calls and close the single step-owned client."""
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()


async def _close_classifier(classifier: Classifier) -> None:
    # Finish cleanup even if another cancellation arrives while closing sockets.
    closing = asyncio.create_task(classifier.aclose())
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError as cancellation:
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            closing.result()
        except BaseException as error:
            cancellation.add_note(f"Classifier cleanup failed: {_error_description(error)}")
        raise


@dataclass(frozen=True)
class _ClassifierStepSpec:
    user_fn: Callable[..., object]
    declaration_json: str
    input_model: type[BaseModel] | None
    runtime_overrides: ClassifierRuntime
    public_signature: inspect.Signature

    def declaration(
        self, workflow_defaults: Mapping[str, JsonValue] | None = None
    ) -> ClassifierDeclaration:
        runtime = {
            **validate_runtime_defaults(dict(workflow_defaults or {})),
            **self.runtime_overrides.model_dump(mode="json", exclude_unset=True),
        }
        declaration = ClassifierDeclaration.model_validate_json(self.declaration_json)
        return declaration.model_copy(
            update={"runtime": ClassifierRuntime.model_validate(runtime)}
        )

    def declaration_metadata(
        self, workflow_defaults: Mapping[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Return an owned declaration without consulting credentials or the network."""
        return self.declaration(workflow_defaults).model_dump(mode="json")

    def make_classifier(self) -> Classifier:
        return Classifier(
            declaration=self.declaration(_WORKFLOW_CLASSIFIER_DEFAULTS.get()),
            step_name=self.user_fn.__qualname__,
            input_model=self.input_model,
        )

    def with_workflow_defaults(
        self, fn: Callable[..., object], defaults: Mapping[str, JsonValue]
    ) -> Callable[..., object]:
        owned_defaults = validate_runtime_defaults(dict(defaults))

        async def bound(*args: object, **kwargs: object) -> object:
            # Cloudpickle must resolve this ContextVar in the worker, not capture it.
            from avalanche.classifier.classifier_step import _WORKFLOW_CLASSIFIER_DEFAULTS

            token = _WORKFLOW_CLASSIFIER_DEFAULTS.set(owned_defaults)
            try:
                result = fn(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            finally:
                _WORKFLOW_CLASSIFIER_DEFAULTS.reset(token)

        update_wrapper(bound, fn)
        bound.__signature__ = self.public_signature  # type: ignore[attr-defined]
        return bound


def _resolved_annotations(
    user_fn: Callable[..., object], localns: dict[str, object] | None
) -> dict[str, object]:
    try:
        return get_type_hints(user_fn, localns=localns, include_extras=True)
    except (NameError, TypeError, SyntaxError):
        # One unresolved annotation must not hide all the other declared types.
        resolved: dict[str, object] = {}
        for name, annotation in inspect.get_annotations(user_fn).items():
            try:
                resolved[name] = get_type_hints(
                    SimpleNamespace(__annotations__={name: annotation}),
                    globalns=user_fn.__globals__,
                    localns=localns,
                    include_extras=True,
                )[name]
            except (NameError, TypeError, SyntaxError):
                resolved[name] = annotation
        return resolved


def _public_step_signature(
    user_fn: Callable[..., object], localns: dict[str, object] | None
) -> inspect.Signature:
    signature = inspect.signature(user_fn)
    annotations = _resolved_annotations(user_fn, localns)
    parameter = signature.parameters.get("classifier")
    if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise ClassifierStepError(
            f"classifier step {user_fn.__qualname__!r} requires "
            "a keyword-only classifier parameter"
        )
    if parameter.default is not inspect.Parameter.empty:
        raise ClassifierStepError("classifier is framework-injected and cannot have a default")
    if annotations.get("classifier") is not Classifier:
        raise ClassifierStepError("classifier parameter must be annotated ava.Classifier")
    return signature.replace(
        parameters=[
            value.replace(annotation=annotations.get(name, value.annotation))
            for name, value in signature.parameters.items()
            if name != "classifier"
        ],
        return_annotation=annotations.get("return", signature.return_annotation),
    )


def _annotation_name(annotation: object) -> str:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _annotation_name(arguments[0])
    if origin in (Union, UnionType):
        return " | ".join(_annotation_name(argument) for argument in arguments)
    if origin is not None and origin is not Literal:
        parameters = ", ".join(_annotation_name(argument) for argument in arguments)
        return f"{_annotation_name(origin)}[{parameters}]"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        return annotation.__name__
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _annotation_schema(annotation: object, *, mode: JsonSchemaMode) -> ClassifierStepOutput:
    if annotation is inspect.Signature.empty:
        return ClassifierStepOutput()
    type_name = _annotation_name(annotation)
    try:
        schema = TypeAdapter(annotation).json_schema(mode=mode)
    except (PydanticUserError, NameError, TypeError, ValueError):
        schema = None
    return ClassifierStepOutput(type_name=type_name, json_schema=schema)


def _step_inputs(signature: inspect.Signature) -> list[ClassifierStepInput]:
    from ..runtime import BaseContext, BaseInput
    from ..runtime.providers import PROVIDERS

    inputs: list[ClassifierStepInput] = []
    for parameter in signature.parameters.values():
        runtime_type = parameter.annotation
        if get_origin(runtime_type) is Annotated:
            runtime_type = get_args(runtime_type)[0]
        if _safe_issubclass(runtime_type, (BaseContext, BaseInput)):
            continue
        if _matching_provider(parameter.default, PROVIDERS) is not None:
            continue
        annotation = _annotation_schema(parameter.annotation, mode="validation")
        inputs.append(
            ClassifierStepInput(
                name=parameter.name,
                type_name=annotation.type_name,
                json_schema=annotation.json_schema,
                required=parameter.default is inspect.Parameter.empty
                and parameter.kind
                not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD),
            )
        )
    return inputs


def classifier_step(
    *,
    questions: Mapping[str, object],
    input_model: type[BaseModel] | None = None,
    model: str | None = None,
    timeout: float | None = None,
    slug: str | None = None,
) -> Callable[[Callable[..., object]], Node]:
    """Declare fixed TypeSafe questions on an ordinary, bodyful workflow step."""
    try:
        validated_questions = validate_questions(questions)
        if input_model is not None and not _safe_issubclass(input_model, BaseModel):
            raise TypeError("input_model must be a Pydantic model class")
        input_schema = (
            TypeAdapter(input_model).json_schema(mode="serialization")
            if input_model is not None
            else None
        )
        overrides: dict[str, object] = {}
        if model is not None:
            overrides["model"] = model
        if timeout is not None:
            overrides["timeout"] = timeout
        runtime_overrides = ClassifierRuntime.model_validate(overrides)
    except (PydanticUserError, ValidationError, ValueError, TypeError) as error:
        raise ClassifierStepError(
            f"invalid classifier declaration: {_error_description(error)}"
        ) from None

    def decorator(user_fn: Callable[..., object]) -> Node:
        frame = inspect.currentframe()
        try:
            localns = frame.f_back.f_locals if frame is not None and frame.f_back else None
            public_signature = _public_step_signature(user_fn, localns)
        finally:
            del frame
        declaration = ClassifierDeclaration(
            questions=validated_questions,
            runtime=runtime_overrides,
            input_schema=input_schema,
            step_inputs=_step_inputs(public_signature),
            step_output=_annotation_schema(
                public_signature.return_annotation, mode="serialization"
            ),
        )
        spec = _ClassifierStepSpec(
            user_fn=user_fn,
            declaration_json=declaration.model_dump_json(),
            input_model=input_model,
            runtime_overrides=runtime_overrides,
            public_signature=public_signature,
        )

        async def wrapper(*args: object, **kwargs: object) -> object:
            if "classifier" in kwargs:
                raise ClassifierStepError(
                    "classifier is framework-injected and cannot be overridden"
                )
            classifier = spec.make_classifier()
            primary_error: BaseException | None = None
            try:
                result = user_fn(*args, **kwargs, classifier=classifier)
                return await result if inspect.isawaitable(result) else result
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    await _close_classifier(classifier)
                except BaseException as close_error:
                    if primary_error is not None:
                        primary_error.add_note(
                            f"Classifier cleanup failed: {_error_description(close_error)}"
                        )
                    elif isinstance(close_error, Exception):
                        raise ClassifierStepExecutionError(
                            f"classifier cleanup failed: {_error_description(close_error)}"
                        ) from None
                    else:
                        raise

        update_wrapper(wrapper, user_fn)
        wrapper.__signature__ = spec.public_signature  # type: ignore[attr-defined]
        wrapper.__classifier_step__ = spec  # type: ignore[attr-defined]
        return Node(wrapper, NodeType.STEP, num_returns=1, slug=slug)

    return decorator
