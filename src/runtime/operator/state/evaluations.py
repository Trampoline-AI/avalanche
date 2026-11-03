"""Adapter for ``run_evaluations``: evaluation records in submission order.

A row holds one :class:`EvaluationRecord` as JSON, with its node and status as
columns for filtering. A record is inserted ``pending`` when the coordinator
submits it and replaced when it completes or fails.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict

from ..evaluation_models import EvaluationRecord
from .errors import StateRowError
from .rows import json_column, row_model

_TABLE = "run_evaluations"


class _RecordRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    record_json: str


class RunEvaluationsTable:
    """Read and write ``run_evaluations`` rows inside a store transaction."""

    @staticmethod
    def insert(connection: sqlite3.Connection, record: EvaluationRecord) -> None:
        """Append a new record after the run's earlier ones."""
        connection.execute(
            f"INSERT INTO {_TABLE} "
            "(run_id, evaluation_id, position, node_id, status, record_json) "
            f"VALUES (?, ?, (SELECT COUNT(*) FROM {_TABLE} WHERE run_id = ?), ?, ?, ?)",
            (
                record.run_id,
                record.evaluation_id,
                record.run_id,
                record.node_id,
                record.status,
                record.model_dump_json(),
            ),
        )

    @staticmethod
    def replace(connection: sqlite3.Connection, record: EvaluationRecord) -> None:
        """Replace a stored record with its completed or failed version."""
        cursor = connection.execute(
            f"UPDATE {_TABLE} SET status = ?, record_json = ? "
            "WHERE run_id = ? AND evaluation_id = ? AND node_id = ?",
            (
                record.status,
                record.model_dump_json(),
                record.run_id,
                record.evaluation_id,
                record.node_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateRowError(
                f"Evaluation {record.evaluation_id} of run {record.run_id} is not stored"
            )

    @staticmethod
    def get(
        connection: sqlite3.Connection, run_id: str, evaluation_id: str
    ) -> EvaluationRecord | None:
        row = connection.execute(
            f"SELECT record_json FROM {_TABLE} WHERE run_id = ? AND evaluation_id = ?",
            (run_id, evaluation_id),
        ).fetchone()
        return None if row is None else _record(row)

    @staticmethod
    def list_run(
        connection: sqlite3.Connection, run_id: str, node_id: str = ""
    ) -> tuple[EvaluationRecord, ...]:
        """A run's records in submission order, optionally only one node's."""
        node_filter = " AND node_id = ?" if node_id else ""
        rows = connection.execute(
            f"SELECT record_json FROM {_TABLE} WHERE run_id = ?{node_filter} ORDER BY position",
            (run_id, *((node_id,) if node_id else ())),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    @staticmethod
    def list_pending(connection: sqlite3.Connection) -> tuple[EvaluationRecord, ...]:
        """Every record still pending, in run and submission order."""
        rows = connection.execute(
            f"SELECT record_json FROM {_TABLE} WHERE status = 'pending' "
            "ORDER BY run_id, position"
        ).fetchall()
        return tuple(_record(row) for row in rows)

    @staticmethod
    def delete_run(connection: sqlite3.Connection, run_id: str) -> int:
        """Remove a run's evaluation records; return how many were removed."""
        return connection.execute(f"DELETE FROM {_TABLE} WHERE run_id = ?", (run_id,)).rowcount


def _record(row: sqlite3.Row) -> EvaluationRecord:
    stored = row_model(_RecordRow, row, table=_TABLE)
    return json_column(EvaluationRecord, stored.record_json, table=_TABLE, column="record_json")
