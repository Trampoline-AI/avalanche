"""Schema conversion utilities for Lance."""

from __future__ import annotations

from typing import Type

import dataframely as dy
import pyarrow as pa


def dataframely_to_lance_schema(schema: Type[dy.Schema]) -> pa.Schema:
    """Convert a DataFramely schema to the PyArrow schema Lance expects."""
    return schema.pyarrow_schema()


def normalize_schema(schema: Type[dy.Schema] | pa.Schema) -> pa.Schema:
    """Normalize supported Avalanche schema declarations for Lance."""
    if isinstance(schema, pa.Schema):
        return schema

    if isinstance(schema, type) and issubclass(schema, dy.Schema):
        return dataframely_to_lance_schema(schema)

    raise TypeError(
        f"Schema must be either PyArrow Schema or DataFramely Schema class, got {type(schema)}"
    )
