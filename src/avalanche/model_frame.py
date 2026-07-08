"""Pydantic model conversion helpers for Arrow-backed model frames."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, IntEnum, StrEnum
from typing import Annotated, Any, Sequence, Union, final, get_args, get_origin

import polars as pl
import pyarrow as pa
from pydantic import BaseModel, TypeAdapter


@final
class Json:
    """Mark a pydantic field to be stored as a JSON string column."""


class UnsupportedModelFieldError(ValueError):
    """Raised when a pydantic field annotation cannot be represented in Arrow."""


@dataclass(frozen=True)
class _ValueSpec:
    is_json: bool = False
    fields: tuple[_FieldSpec, ...] = ()
    item: _ValueSpec | None = None


@dataclass(frozen=True)
class _FieldSpec:
    name: str
    arrow_field: pa.Field
    value: _ValueSpec


_UNION_ORIGINS = (Union, types.UnionType)


def model_to_arrow_schema(model: type[BaseModel]) -> pa.Schema:
    """Return the Arrow schema for a pydantic model class."""
    specs = _model_specs(model)
    return pa.schema([spec.arrow_field for spec in specs])


def models_to_arrow(models: Sequence[BaseModel], model: type[BaseModel]) -> pa.Table:
    """Serialize pydantic model instances into a PyArrow table."""
    specs = _model_specs(model)
    schema = pa.schema([spec.arrow_field for spec in specs])
    rows = []
    for item in models:
        python_row = item.model_dump(mode="python")
        json_row = item.model_dump(mode="json")
        rows.append(_dump_fields(python_row, json_row, specs))

    return pa.Table.from_pylist(rows, schema=schema)


def arrow_to_models(data: pa.Table | pl.DataFrame, model: type[BaseModel]) -> list[BaseModel]:
    """Deserialize a PyArrow table or Polars DataFrame into pydantic models."""
    table = data.to_arrow() if isinstance(data, pl.DataFrame) else data
    specs = _model_specs(model)

    models = []
    for row in table.to_pylist():
        model_row = {
            spec.name: _load_json_value(row[spec.name], spec.value)
            for spec in specs
            if spec.name in row
        }
        models.append(model.model_validate(model_row))
    return models


def _model_specs(model: type[BaseModel], parent_path: str = "") -> tuple[_FieldSpec, ...]:
    specs = []
    for name, field_info in model.model_fields.items():
        annotation = field_info.annotation
        metadata = tuple(field_info.metadata)
        path = f"{parent_path}.{name}" if parent_path else name
        specs.append(
            _field_spec(
                name=name,
                annotation=annotation,
                field_info_metadata=metadata,
                description=field_info.description,
                path=path,
            )
        )
    return tuple(specs)


def _field_spec(
    *,
    name: str,
    annotation: Any,
    field_info_metadata: tuple[Any, ...],
    description: str | None,
    path: str,
) -> _FieldSpec:
    annotation, annotated_metadata = _unwrap_annotated(annotation)
    metadata = (*field_info_metadata, *annotated_metadata)

    if _has_json_marker(metadata):
        nullable = _lenient_nullable(annotation)
        arrow_type = pa.string()
        value = _ValueSpec(is_json=True)
    else:
        annotation, nullable = _unwrap_optional(annotation, path)
        arrow_type, value = _arrow_type_for_annotation(annotation, path)

    return _FieldSpec(
        name=name,
        arrow_field=pa.field(
            name,
            arrow_type,
            nullable=nullable,
            metadata=_description_metadata(description),
        ),
        value=value,
    )


def _arrow_type_for_annotation(annotation: Any, path: str) -> tuple[pa.DataType, _ValueSpec]:
    annotation, metadata = _unwrap_annotated(annotation)

    if _has_json_marker(metadata):
        return pa.string(), _ValueSpec(is_json=True)

    annotation, _ = _unwrap_optional(annotation, path)

    if annotation is Any:
        _raise_unsupported(path, annotation)
    if annotation is str:
        return pa.string(), _ValueSpec()
    if annotation is bool:
        return pa.bool_(), _ValueSpec()
    if annotation is int:
        return pa.int64(), _ValueSpec()
    if annotation is float:
        return pa.float64(), _ValueSpec()
    if annotation is datetime:
        return pa.timestamp("us"), _ValueSpec()
    if annotation is date:
        return pa.date32(), _ValueSpec()
    if annotation is bytes:
        return pa.binary(), _ValueSpec()

    origin = get_origin(annotation)
    if _is_union_origin(origin):
        _raise_unsupported(path, annotation)

    if _is_enum(annotation):
        if issubclass(annotation, StrEnum) or issubclass(annotation, str):
            return pa.string(), _ValueSpec()
        if issubclass(annotation, IntEnum) or issubclass(annotation, int):
            return pa.int64(), _ValueSpec()

    if _is_pydantic_model(annotation):
        fields = _model_specs(annotation, parent_path=path)
        return pa.struct([spec.arrow_field for spec in fields]), _ValueSpec(fields=fields)

    if origin is list:
        args = get_args(annotation)
        if not args:
            _raise_unsupported(path, annotation)
        item_annotation, item_metadata = _unwrap_annotated(args[0])
        if _has_json_marker(item_metadata):
            item_type = pa.string()
            item_value = _ValueSpec(is_json=True)
        else:
            item_annotation, _ = _unwrap_optional(item_annotation, path)
            item_type, item_value = _arrow_type_for_annotation(item_annotation, path)
        return pa.list_(item_type), _ValueSpec(item=item_value)

    return _arrow_type_for_custom_scalar(annotation, path)


def _arrow_type_for_custom_scalar(annotation: Any, path: str) -> tuple[pa.DataType, _ValueSpec]:
    try:
        schema = TypeAdapter(annotation).json_schema(mode="serialization")
    except Exception as exc:
        raise _unsupported_error(path, annotation) from exc

    schema_type = schema.get("type")
    if schema_type == "string":
        return pa.string(), _ValueSpec()
    if schema_type == "integer":
        return pa.int64(), _ValueSpec()
    if schema_type == "number":
        return pa.float64(), _ValueSpec()
    if schema_type == "boolean":
        return pa.bool_(), _ValueSpec()

    _raise_unsupported(path, annotation)


def _unwrap_annotated(annotation: Any) -> tuple[Any, tuple[Any, ...]]:
    metadata = []
    while get_origin(annotation) is Annotated:
        args = get_args(annotation)
        annotation = args[0]
        metadata.extend(args[1:])
    return annotation, tuple(metadata)


def _unwrap_optional(annotation: Any, path: str) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if not _is_union_origin(origin):
        return annotation, False

    args = get_args(annotation)
    non_none_args = tuple(arg for arg in args if arg is not types.NoneType)
    if len(non_none_args) == 1:
        return non_none_args[0], True

    _raise_unsupported(path, annotation)


def _lenient_nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    return _is_union_origin(origin) and types.NoneType in get_args(annotation)


def _is_union_origin(origin: Any) -> bool:
    return origin in _UNION_ORIGINS


def _is_enum(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def _is_pydantic_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _has_json_marker(metadata: tuple[Any, ...]) -> bool:
    return any(item is Json or isinstance(item, Json) for item in metadata)


def _description_metadata(description: str | None) -> dict[bytes, bytes] | None:
    if description is None:
        return None
    return {b"description": description.encode("utf-8")}


def _dump_fields(
    python_row: dict[str, Any],
    json_row: dict[str, Any],
    specs: tuple[_FieldSpec, ...],
) -> dict[str, Any]:
    return {
        spec.name: _dump_value(python_row.get(spec.name), json_row.get(spec.name), spec.value)
        for spec in specs
    }


def _dump_value(python_value: Any, json_value: Any, spec: _ValueSpec) -> Any:
    if python_value is None:
        return None
    if spec.is_json:
        return json.dumps(json_value)
    if spec.fields:
        return _dump_fields(python_value, json_value, spec.fields)
    if spec.item is not None:
        return [
            _dump_value(python_item, json_item, spec.item)
            for python_item, json_item in zip(python_value, json_value, strict=True)
        ]
    return python_value


def _load_json_value(value: Any, spec: _ValueSpec) -> Any:
    if value is None:
        return None
    if spec.is_json:
        return json.loads(value)
    if spec.fields:
        return {
            field_spec.name: _load_json_value(value[field_spec.name], field_spec.value)
            for field_spec in spec.fields
            if field_spec.name in value
        }
    if spec.item is not None:
        return [_load_json_value(item, spec.item) for item in value]
    return value


def _unsupported_error(path: str, annotation: Any) -> UnsupportedModelFieldError:
    return UnsupportedModelFieldError(
        f"Field '{path}' has unsupported annotation {annotation!r}; "
        "use Annotated[..., avalanche.Json] to store it as a JSON string column."
    )


def _raise_unsupported(path: str, annotation: Any) -> None:
    raise _unsupported_error(path, annotation)
