"""Adapter for ``operator_metadata``: the single row of operator-wide values."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, NonNegativeInt

from .errors import StateRowError
from .rows import row_model

_TABLE = "operator_metadata"


class OperatorMetadata(BaseModel):
    """When the database was created and the last persisted sequence counters."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    created_at: float
    update_sequence: NonNegativeInt
    catalog_sequence: NonNegativeInt


class OperatorMetadataTable:
    """Read and write the one ``operator_metadata`` row inside a store transaction."""

    @staticmethod
    def read(connection: sqlite3.Connection) -> OperatorMetadata:
        row = connection.execute(
            f"SELECT created_at, update_sequence, catalog_sequence FROM {_TABLE} WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StateRowError(f"Operator state table {_TABLE} has no row")
        return row_model(OperatorMetadata, row, table=_TABLE)

    @staticmethod
    def write_sequences(
        connection: sqlite3.Connection, *, update_sequence: int, catalog_sequence: int
    ) -> None:
        connection.execute(
            f"UPDATE {_TABLE} SET update_sequence = ?, catalog_sequence = ? WHERE id = 1",
            (update_sequence, catalog_sequence),
        )
