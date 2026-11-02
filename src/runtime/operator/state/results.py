"""Adapter for ``run_results``: the index of accepted workflow result bundles.

Each row names the bundle directory that holds one successful run's result,
under the result-storage root next to the database, and the manifest digest the
operator accepted, so a bundle changed on disk later is refused when loaded.
A row is written only once the run has ended in success.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from .rows import row_model

_TABLE = "run_results"
_COLUMNS = "run_id, bundle_name, manifest_sha256, byte_size, published_at"

BUNDLE_NAME_PATTERN = r"^bundle_[0-9a-f]{32}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StoredWorkflowResult(BaseModel):
    """An accepted result bundle: its directory, manifest digest and size."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    bundle_name: str = Field(pattern=BUNDLE_NAME_PATTERN)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    byte_size: NonNegativeInt
    published_at: float


class RunResultRow(StoredWorkflowResult):
    """One ``run_results`` row: the accepted result of one successful run."""

    run_id: str = Field(min_length=1)


class RunResultTable:
    """Read and write ``run_results`` rows inside a store transaction."""

    @staticmethod
    def insert(
        connection: sqlite3.Connection, run_id: str, stored: StoredWorkflowResult
    ) -> RunResultRow:
        row = RunResultRow(run_id=run_id, **stored.model_dump())
        connection.execute(
            f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (
                row.run_id,
                row.bundle_name,
                row.manifest_sha256,
                row.byte_size,
                row.published_at,
            ),
        )
        return row

    @staticmethod
    def get(connection: sqlite3.Connection, run_id: str) -> RunResultRow | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else row_model(RunResultRow, row, table=_TABLE)

    @staticmethod
    def delete(connection: sqlite3.Connection, run_id: str) -> RunResultRow | None:
        row = connection.execute(
            f"DELETE FROM {_TABLE} WHERE run_id = ? RETURNING {_COLUMNS}", (run_id,)
        ).fetchone()
        return None if row is None else row_model(RunResultRow, row, table=_TABLE)

    @staticmethod
    def list_all(connection: sqlite3.Connection) -> tuple[RunResultRow, ...]:
        rows = connection.execute(f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY run_id").fetchall()
        return tuple(row_model(RunResultRow, row, table=_TABLE) for row in rows)
