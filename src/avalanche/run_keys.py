"""Typed run identity shared by machine and keyed workflow declarations."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, JsonValue, TypeAdapter


def derive_run_key(
    input_type: type[BaseModel],
    fields: tuple[str, ...],
    input: BaseModel | Mapping[str, JsonValue],
) -> tuple[JsonValue, ...]:
    """Validate only identity fields; an existing run ignores replacement resources."""
    values = []
    for name in fields:
        field = input_type.model_fields[name]
        if isinstance(input, BaseModel):
            value = getattr(input, name)
        elif name in input:
            value = input[name]
        elif not field.is_required():
            value = field.get_default(call_default_factory=True)
        else:
            raise ValueError(f"missing run key field {name!r}")
        adapter = TypeAdapter(field.rebuild_annotation())
        values.append(adapter.dump_python(adapter.validate_python(value), mode="json"))
    return tuple(values)
