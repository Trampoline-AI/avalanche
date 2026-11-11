"""Portable typed run identity. Declarations contain data, never tenant code."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, JsonValue, TypeAdapter
from pydantic_core import SchemaValidator

# Admission-path work bounds apply to both deployed declarations and input values.
MAX_KEY_DEPTH = 32
MAX_KEY_NODES = 4096
MAX_SCHEMA_NODES = 512


def bounded(value, *, limit=MAX_KEY_NODES):
    pending = [(value, 0)]
    remaining = limit
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_KEY_DEPTH:
            raise ValueError("run_key_input_bound")
        if isinstance(item, (dict, list, tuple, set, frozenset)):
            if len(item) > remaining - len(pending):
                raise ValueError("run_key_input_bound")
            values = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in values)


def _plain_model(model, names, path):
    decorators = model.__pydantic_decorators__
    if decorators.model_validators or decorators.model_serializers or decorators.validators:
        raise ValueError(f"run key field {path!r}: custom model validator/serializer")
    if model.model_config.keys() - {"extra", "frozen", "arbitrary_types_allowed", "title"}:
        raise ValueError(f"run key field {path!r}: non-portable model config/alias generator")
    for name in names:
        field = model.model_fields[name]
        if any(
            alias is not None
            for alias in (field.alias, field.validation_alias, field.serialization_alias)
        ):
            raise ValueError(f"run key field {path}.{name}: aliases are not portable")
        if field.default_factory is not None:
            raise ValueError(f"run key field {path}.{name}: default factories are not portable")
        for decorator in (
            *decorators.field_validators.values(),
            *decorators.field_serializers.values(),
        ):
            if name in decorator.info.fields or "*" in decorator.info.fields:
                raise ValueError(f"run key field {path}.{name}: custom validator/serializer")


# Only these builtin core-schema nodes can cross the deployment boundary.
# Unknown nodes (including function validators/serializers) fail closed at build.
_OPTIONS = {
    "str": {
        "strict",
        "min_length",
        "max_length",
        "pattern",
        "strip_whitespace",
        "to_lower",
        "to_upper",
    },
    "int": {"strict", "gt", "ge", "lt", "le", "multiple_of"},
    "float": {"strict", "gt", "ge", "lt", "le", "multiple_of", "allow_inf_nan"},
    "bool": {"strict"},
    "none": set(),
    "uuid": {"strict", "version"},
    "date": {"strict", "gt", "ge", "lt", "le"},
    "datetime": {"strict", "microseconds_precision", "tz_constraint", "gt", "ge", "lt", "le"},
    "literal": {"expected"},
    "enum": {"sub_type", "strict"},
    "nullable": set(),
    "union": {"mode", "auto_collapse"},
    "tuple": {"strict", "min_length", "max_length", "variadic_item_index"},
    "set": {"strict", "min_length", "max_length"},
    "frozenset": {"strict", "min_length", "max_length"},
    "list": {"strict", "min_length", "max_length"},
    "dict": {"strict", "min_length", "max_length"},
    "default": {"default", "validate_default", "on_error"},
    "model": {"config", "custom_init", "root_model"},
    "model-fields": {"model_name", "extra_behavior"},
    "model-field": {"frozen"},
}
_CHILDREN = {"schema", "items_schema", "keys_schema", "values_schema", "choices", "fields"}


def _export(schema, path, definitions, budget, depth=0):
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_KEY_DEPTH:
        raise ValueError(f"run key field {path!r}: run_key_schema_bound")
    kind = schema["type"]
    if kind == "definitions":
        definitions = {**definitions, **{item["ref"]: item for item in schema["definitions"]}}
        return _export(schema["schema"], path, definitions, budget, depth + 1)
    if kind == "definition-ref":
        return _export(definitions[schema["schema_ref"]], path, definitions, budget, depth + 1)
    if kind not in _OPTIONS or "serialization" in schema:
        raise ValueError(
            f"run key field {path!r}: unsupported type or custom validator/serializer ({kind})"
        )
    ignored = {"metadata", "ref", "cls", "members", "computed_fields"}
    if schema.keys() - (_OPTIONS[kind] | _CHILDREN | ignored | {"type"}):
        raise ValueError(f"run key field {path!r}: unsupported {kind} declaration")
    result = {key: value for key, value in schema.items() if key in _OPTIONS[kind] | {"type"}}
    if kind == "model":
        model = schema["cls"]
        _plain_model(model, model.model_fields, path)
        if schema.get("custom_init") or schema.get("root_model"):
            raise ValueError(f"run key field {path!r}: only plain nested models are portable")
    if kind == "model-fields" and schema.get("computed_fields"):
        raise ValueError(f"run key field {path!r}: computed fields are not portable")
    if kind == "default":
        result["default"] = canonical_value(schema["default"])
        result["validate_default"] = True
    if kind == "enum":
        result["members"] = [member.value for member in schema["members"]]
        if any(
            type(item) not in (str, int, float, bool, type(None)) for item in result["members"]
        ):
            raise ValueError(f"run key field {path!r}: unsupported enum value")
    for key in _CHILDREN & schema.keys():
        child = schema[key]
        if key == "fields":
            result[key] = {
                name: _export(item, f"{path}.{name}", definitions, budget, depth + 1)
                for name, item in child.items()
            }
        elif isinstance(child, list):
            result[key] = [
                _export(item, path, definitions, budget, depth + 1) for item in child
            ]
        else:
            result[key] = _export(child, path, definitions, budget, depth + 1)
    # Reject Python-only defaults/constraints instead of letting a manifest lie.
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"run key field {path!r}: non-portable default/constraint") from exc
    return result


def _check_annotation(annotation, path, seen):
    origin = get_origin(annotation)
    if origin is Annotated:
        return _check_annotation(get_args(annotation)[0], path, seen)
    if origin in (Union, UnionType, tuple, set, frozenset, list, dict):
        for child in get_args(annotation):
            if child is not Ellipsis:
                _check_annotation(child, path, seen)
        return
    if origin is Literal or annotation in (
        str,
        int,
        float,
        bool,
        type(None),
        UUID,
        date,
        datetime,
    ):
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            raise ValueError(f"run key field {path!r}: recursive models are not portable")
        if "__get_pydantic_core_schema__" in annotation.__dict__:
            raise ValueError(f"run key field {path!r}: custom model schema is not portable")
        for name, field in annotation.model_fields.items():
            _check_annotation(field.annotation, f"{path}.{name}", seen | {annotation})
        return
    raise ValueError(f"run key field {path!r}: unsupported type {annotation!r}")


def key_declarations(input_type, fields) -> dict[str, JsonValue]:
    """Check a declaration, exporting only identity fields, before any host runs it."""
    if not fields:
        return {}
    if input_type is None or not set(fields) <= input_type.model_fields.keys():
        raise ValueError(f"run key fields {fields!r} must be declared input fields")
    if len(fields) != len(set(fields)):
        raise ValueError("duplicate run key fields")
    _plain_model(input_type, fields, ",".join(fields))
    declarations = {}
    budget = [MAX_SCHEMA_NODES]
    for name in fields:
        field = input_type.model_fields[name]
        _check_annotation(field.annotation, name, set())
        schema = TypeAdapter(field.rebuild_annotation()).core_schema
        if not field.is_required():
            schema = {
                "type": "default",
                "schema": schema,
                "default": field.default,
                "validate_default": True,
            }
        declarations[name] = _export(schema, name, {}, budget)
    bounded(declarations, limit=MAX_SCHEMA_NODES)
    return declarations


class _KeyModel:
    __slots__ = (
        "__dict__",
        "__pydantic_extra__",
        "__pydantic_private__",
        "__pydantic_fields_set__",
    )


def _compile_portable(schema):
    kind = schema["type"]
    if kind not in _OPTIONS:
        raise ValueError("unsupported_run_key_declaration")
    result = {key: value for key, value in schema.items() if key in _OPTIONS[kind] | {"type"}}
    if kind == "enum":
        subtype = schema.get("sub_type")
        mixin = {"str": str, "int": int, "float": float}.get(subtype)
        cls = Enum(
            "RunKeyEnum",
            {f"v{i}": value for i, value in enumerate(schema["members"])},
            **({"type": mixin} if mixin is not None else {}),
        )
        result.update(cls=cls, members=list(cls))
    if kind == "model":
        result["cls"] = type("RunKeyModel", (_KeyModel,), {})
    for key in _CHILDREN & schema.keys():
        child = schema[key]
        if key == "fields":
            result[key] = {name: _compile_portable(item) for name, item in child.items()}
        elif isinstance(child, list):
            result[key] = [_compile_portable(item) for item in child]
        else:
            result[key] = _compile_portable(child)
    return result


def _native(value):
    if isinstance(value, BaseModel):
        return {
            name: _native(item)
            for name, item in {**value.__dict__, **(value.__pydantic_extra__ or {})}.items()
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {name: _native(item) for name, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_native(item) for item in value]
    return value


def canonical_value(value):
    """JSON identity, with UTC datetimes and recursively sorted unordered values."""
    if isinstance(value, (BaseModel, _KeyModel)):
        return {
            name: canonical_value(item)
            for name, item in {**value.__dict__, **(value.__pydantic_extra__ or {})}.items()
        }
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(name): canonical_value(item) for name, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((canonical_value(item) for item in value), key=canonical_json)
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def derive_portable_key(declarations, fields, inputs):
    bounded(declarations, limit=MAX_SCHEMA_NODES)
    values = []
    for name in fields:
        schema = declarations[name]
        raw = inputs[name] if name in inputs else schema.get("default")
        if name not in inputs and "default" not in schema:
            raise ValueError(f"missing run key field {name!r}")
        bounded(raw)
        value = canonical_value(SchemaValidator(_compile_portable(schema)).validate_python(raw))
        bounded(value)
        canonical_json(value)  # Reject non-finite identity values on every host.
        values.append(value)
    return values


def derive_run_key(
    input_type: type[BaseModel],
    fields: tuple[str, ...],
    input: BaseModel | Mapping[str, JsonValue],
) -> tuple[JsonValue, ...]:
    """Validate identity only; replacement non-key input/resources remain irrelevant."""
    declarations = key_declarations(input_type, fields)
    inputs = (
        {name: _native(getattr(input, name)) for name in fields}
        if isinstance(input, BaseModel)
        else input
    )
    return tuple(derive_portable_key(declarations, fields, inputs))
