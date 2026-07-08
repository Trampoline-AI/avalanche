"""Schema conversion utilities for Lance."""

from __future__ import annotations

from typing import Type

import dataframely as dy
import pyarrow as pa
from pydantic import BaseModel

from avalanche.model_frame import model_to_arrow_schema


def dataframely_to_lance_schema(schema: Type[dy.Schema]) -> pa.Schema:
    """Convert a DataFramely schema to the PyArrow schema Lance expects."""
    return schema.pyarrow_schema()


def normalize_schema(schema: Type[dy.Schema] | pa.Schema | type[BaseModel]) -> pa.Schema:
    """Normalize supported Avalanche schema declarations for Lance."""
    if isinstance(schema, pa.Schema):
        return schema

    if isinstance(schema, type) and issubclass(schema, dy.Schema):
        return dataframely_to_lance_schema(schema)

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return model_to_arrow_schema(schema)

    raise TypeError(
        "Schema must be either PyArrow Schema, DataFramely Schema class, "
        f"or pydantic BaseModel class, got {type(schema)}"
    )
