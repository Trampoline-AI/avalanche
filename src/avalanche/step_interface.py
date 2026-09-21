"""Serializable Python function interfaces, separate from injected runtime dependencies."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import SimpleNamespace, UnionType
from typing import Annotated, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PydanticUserError, TypeAdapter
from pydantic.json_schema import JsonSchemaMode


class _StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class StepOutput(_StepModel):
    type_name: str = "Unspecified"
    json_schema: dict[str, JsonValue] | None = None


class StepInput(StepOutput):
    name: str
    required: bool


class StepInterface(_StepModel):
    step_inputs: list[StepInput] = Field(default_factory=list)
    step_output: StepOutput = Field(default_factory=StepOutput)


def decoration_namespace() -> dict[str, object] | None:
    """Read the scope applying a decorator, without retaining its frame on the node."""
    frame = inspect.currentframe()
    try:
        decorator = frame.f_back if frame is not None else None
        return decorator.f_back.f_locals if decorator and decorator.f_back else None
    finally:
        del frame


def resolve_step_signature(
    fn: Callable[..., object], localns: dict[str, object] | None = None
) -> inspect.Signature:
    signature = inspect.signature(fn)
    user_fn = inspect.unwrap(fn)
    try:
        annotations = get_type_hints(user_fn, localns=localns, include_extras=True)
    except (NameError, TypeError, SyntaxError):
        # One unresolved annotation must not hide the other declared types.
        annotations = {}
        for name, annotation in inspect.get_annotations(user_fn).items():
            try:
                annotations[name] = get_type_hints(
                    SimpleNamespace(__annotations__={name: annotation}),
                    globalns=user_fn.__globals__,
                    localns=localns,
                    include_extras=True,
                )[name]
            except (NameError, TypeError, SyntaxError):
                annotations[name] = annotation
    return signature.replace(
        parameters=[
            parameter.replace(annotation=annotations.get(name, parameter.annotation))
            for name, parameter in signature.parameters.items()
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


def _annotation_schema(annotation: object, *, mode: JsonSchemaMode) -> StepOutput:
    if annotation is inspect.Signature.empty:
        return StepOutput()
    type_name = _annotation_name(annotation)
    try:
        schema = TypeAdapter(annotation).json_schema(mode=mode)
    except (PydanticUserError, NameError, TypeError, ValueError):
        schema = None
    return StepOutput(type_name=type_name, json_schema=schema)


def step_interface_from_signature(signature: inspect.Signature) -> StepInterface:
    from .dag import _matching_provider, _safe_issubclass
    from .runtime import BaseContext, BaseInput
    from .runtime.providers import PROVIDERS

    inputs: list[StepInput] = []
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
            StepInput(
                name=parameter.name,
                type_name=annotation.type_name,
                json_schema=annotation.json_schema,
                required=parameter.default is inspect.Parameter.empty
                and parameter.kind
                not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD),
            )
        )
    return StepInterface(
        step_inputs=inputs,
        step_output=_annotation_schema(signature.return_annotation, mode="serialization"),
    )


def step_interface_for_function(
    fn: Callable[..., object], localns: dict[str, object] | None = None
) -> StepInterface:
    return step_interface_from_signature(resolve_step_signature(fn, localns))
