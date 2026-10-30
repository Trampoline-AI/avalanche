"""Bounded, display-only previews of what a standard step received and returned.

Previews are built inside the task that runs the step, where its arguments and
return value already exist, so capture never fetches a Ray object or copies a
payload to the coordinator. Each preview walks at most a fixed number of values
and characters; anything cut is replaced by an explicit ``…`` marker and the
preview is flagged ``truncated``. Values that are not plain data (data frames,
arrays, custom objects) are shown as a bounded ``repr`` instead of serialized.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import PurePath
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from avalanche.runtime.providers import LoggerInstance

MAX_PREVIEW_JSON_CHARS = 32_768
MAX_TYPE_NAME_CHARS = 256
MAX_STEP_PARAMETERS = 256

_MAX_PREVIEW_VALUES = 200
_MAX_PREVIEW_CHARS = 8_000
_MAX_CONTAINER_ITEMS = 20
_MAX_DEPTH = 6
_MAX_STRING_CHARS = 512
_MAX_KEY_CHARS = 128
_MAX_REPR_CHARS = 2_000
_ELLIPSIS = "…"


class StepValuePreviewKind(StrEnum):
    """How ``preview_json`` was produced."""

    JSON = "json"
    """A bounded JSON rendering of plain data."""
    REPR = "repr"
    """A JSON string holding the bounded ``repr`` of an opaque object."""


class StepValuePreview(BaseModel):
    """A bounded rendering of one value; never the value itself."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: StepValuePreviewKind
    type_name: str = Field(max_length=MAX_TYPE_NAME_CHARS)
    preview_json: str = Field(max_length=MAX_PREVIEW_JSON_CHARS)
    truncated: bool


class StepParameterPreview(BaseModel):
    """The preview of one argument, keyed by the parameter that received it."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=MAX_TYPE_NAME_CHARS)
    value: StepValuePreview


class StepInputs(BaseModel):
    """Every data argument a step call received, in signature order."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    parameters: tuple[StepParameterPreview, ...] = Field(max_length=MAX_STEP_PARAMETERS)


class StepIo(BaseModel):
    """The latest call's inputs, and its output once the call has returned."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    inputs: tuple[StepParameterPreview, ...] = Field(max_length=MAX_STEP_PARAMETERS)
    output: StepValuePreview | None = None


@dataclass
class _Budget:
    values: int = _MAX_PREVIEW_VALUES
    chars: int = _MAX_PREVIEW_CHARS
    truncated: bool = False


def preview_value(value: object) -> StepValuePreview:
    """Render ``value`` within the fixed preview budget."""
    budget = _Budget()
    opaque = not _is_plain_data(value)
    rendered = _bounded_repr(value, budget) if opaque else _preview(value, 0, budget)
    return StepValuePreview(
        kind=StepValuePreviewKind.REPR if opaque else StepValuePreviewKind.JSON,
        type_name=_type_name(value),
        preview_json=json.dumps(rendered, ensure_ascii=False, allow_nan=False),
        truncated=budget.truncated,
    )


def preview_step_inputs(
    signature: inspect.Signature,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> StepInputs:
    """Preview each data argument by parameter name, omitting injected loggers."""
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        # The call itself will report the mismatch; still show what arrived.
        named: list[tuple[str, object]] = [
            *((f"args[{index}]", value) for index, value in enumerate(args)),
            *kwargs.items(),
        ]
    else:
        named = list(bound.arguments.items())
    return StepInputs(
        parameters=tuple(
            StepParameterPreview(name=name, value=preview_value(value))
            for name, value in named[:MAX_STEP_PARAMETERS]
            if not isinstance(value, LoggerInstance)
        )
    )


def _is_plain_data(value: object) -> bool:
    return (
        value is None
        or isinstance(
            value,
            (
                bool,
                int,
                float,
                str,
                Enum,
                date,
                time,
                UUID,
                Decimal,
                PurePath,
                BaseModel,
                Mapping,
                list,
                tuple,
                set,
                frozenset,
            ),
        )
        or (dataclasses.is_dataclass(value) and not isinstance(value, type))
    )


def _type_name(value: object) -> str:
    kind = type(value)
    name = (
        kind.__qualname__
        if kind.__module__ == "builtins"
        else f"{kind.__module__}.{kind.__qualname__}"
    )
    return name[:MAX_TYPE_NAME_CHARS]


def _bounded_text(text: str, limit: int, budget: _Budget) -> str:
    allowed = max(0, min(limit, budget.chars))
    if len(text) <= allowed:
        budget.chars -= len(text)
        return text
    budget.chars -= allowed
    budget.truncated = True
    return f"{text[:allowed]}{_ELLIPSIS} [+{len(text) - allowed} chars]"


def _bounded_repr(value: object, budget: _Budget) -> str:
    try:
        text = repr(value)
    except Exception as exc:
        # A broken __repr__ must not fail the step whose value it describes.
        text = f"<{_type_name(value)}: repr failed with {type(exc).__name__}>"
    return _bounded_text(text, _MAX_REPR_CHARS, budget)


def _preview(value: object, depth: int, budget: _Budget) -> JsonValue:
    budget.values -= 1
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return _bounded_text(value, _MAX_STRING_CHARS, budget)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal, PurePath)):
        return _bounded_text(str(value), _MAX_STRING_CHARS, budget)
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        return _preview_mapping(
            len(fields), ((name, getattr(value, name)) for name in fields), depth, budget
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.fields(value)
        return _preview_mapping(
            len(fields),
            ((field.name, getattr(value, field.name)) for field in fields),
            depth,
            budget,
        )
    if isinstance(value, Mapping):
        return _preview_mapping(len(value), iter(value.items()), depth, budget)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _preview_sequence(value, depth, budget)
    return _bounded_repr(value, budget)


def _preview_mapping(
    size: int,
    items: Iterator[tuple[object, object]],
    depth: int,
    budget: _Budget,
) -> JsonValue:
    if depth >= _MAX_DEPTH:
        budget.truncated = True
        return f"{_ELLIPSIS} [{size} nested keys]"
    rendered: dict[str, JsonValue] = {}
    for index, (key, item) in enumerate(items):
        if index >= _MAX_CONTAINER_ITEMS or budget.values <= 0 or budget.chars <= 0:
            budget.truncated = True
            rendered[_ELLIPSIS] = f"+{size - index} more keys"
            break
        label = key if isinstance(key, str) else _bounded_repr(key, budget)
        rendered[_bounded_text(label, _MAX_KEY_CHARS, budget)] = _preview(
            item, depth + 1, budget
        )
    return rendered


def _preview_sequence(
    value: list[object] | tuple[object, ...] | set[object] | frozenset[object],
    depth: int,
    budget: _Budget,
) -> JsonValue:
    if depth >= _MAX_DEPTH:
        budget.truncated = True
        return f"{_ELLIPSIS} [{len(value)} nested items]"
    rendered: list[JsonValue] = []
    for index, item in enumerate(value):
        if index >= _MAX_CONTAINER_ITEMS or budget.values <= 0 or budget.chars <= 0:
            budget.truncated = True
            rendered.append(f"{_ELLIPSIS} [+{len(value) - index} more items]")
            break
        rendered.append(_preview(item, depth + 1, budget))
    return rendered
