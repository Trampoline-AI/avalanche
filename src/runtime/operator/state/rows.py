"""Helpers that turn ``sqlite3`` rows and JSON columns into typed models.

These are the only places a raw column value is read. Every adapter passes its
rows through :func:`row_model` and its JSON columns through :func:`json_column`,
so a stored value that does not match its model raises :class:`StateRowError`
naming the table instead of flowing on as an untyped value.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ValidationError

from .errors import StateRowError


def row_model[M: BaseModel](model: type[M], row: sqlite3.Row, *, table: str) -> M:
    """Validate one row, keyed by column name, into its row model."""
    try:
        return model.model_validate({column: row[column] for column in row.keys()})
    except ValidationError as exc:
        raise StateRowError(f"Invalid row in operator state table {table}: {exc}") from exc


def json_column[M: BaseModel](model: type[M], raw: str, *, table: str, column: str) -> M:
    """Validate one JSON column into its domain model."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise StateRowError(
            f"Invalid JSON in operator state column {table}.{column}: {exc}"
        ) from exc
