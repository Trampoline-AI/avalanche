"""Adapter for ``run_classifier_events``: each classifier step's invocation snapshots.

A row is a :class:`ClassifierEvent`: its sequence among the step's snapshots,
the invocation it describes, its compact answers (JSON) and the full invocation
body. Bodies are kept with the row; nothing removes them when a run ends.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, TypeAdapter

from ..models import ClassifierAnswerSummary, ClassifierEvent
from .agent_events import NodeEventTotals
from .rows import json_value, row_model

_TABLE = "run_classifier_events"
_COLUMNS = (
    "event_sequence, invocation_id, invocation_index, answers_json, event_json, size_bytes, "
    "event_kind, duration_ms, error"
)
_ANSWERS = TypeAdapter(tuple[ClassifierAnswerSummary, ...])


class _ClassifierEventRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    event_sequence: PositiveInt
    invocation_id: str
    invocation_index: int
    answers_json: str
    event_json: str
    size_bytes: NonNegativeInt
    event_kind: str
    duration_ms: int | None
    # SQLite has no boolean type; the column holds 0 or 1.
    error: bool = Field(strict=False)


class _TotalsRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    node_id: str
    last_sequence: PositiveInt
    size_bytes: NonNegativeInt


class RunClassifierEventsTable:
    """Read and write ``run_classifier_events`` rows inside a store transaction."""

    @staticmethod
    def insert(
        connection: sqlite3.Connection, run_id: str, node_id: str, event: ClassifierEvent
    ) -> None:
        connection.execute(
            f"INSERT INTO {_TABLE} (run_id, node_id, {_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                node_id,
                event.event_sequence,
                event.invocation_id,
                event.invocation_index,
                _ANSWERS.dump_json(event.answers).decode(),
                event.event_json,
                event.size_bytes,
                event.event_kind,
                event.duration_ms,
                int(event.error),
            ),
        )

    @staticmethod
    def get(
        connection: sqlite3.Connection, run_id: str, node_id: str, event_sequence: int
    ) -> ClassifierEvent | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            "WHERE run_id = ? AND node_id = ? AND event_sequence = ?",
            (run_id, node_id, event_sequence),
        ).fetchone()
        return None if row is None else _event(row)

    @staticmethod
    def page(
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
        *,
        after: int,
        through: int,
        limit: int,
        newest_first: bool = False,
    ) -> tuple[ClassifierEvent, ...]:
        """Up to ``limit`` snapshots with ``after < event_sequence <= through``."""
        order = "DESC" if newest_first else "ASC"
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            "WHERE run_id = ? AND node_id = ? AND event_sequence > ? AND event_sequence <= ? "
            f"ORDER BY event_sequence {order} LIMIT ?",
            (run_id, node_id, after, through, limit),
        ).fetchall()
        return tuple(_event(row) for row in rows)

    @staticmethod
    def totals(connection: sqlite3.Connection) -> tuple[NodeEventTotals, ...]:
        """Every step's last snapshot sequence and snapshot size, from its stored rows."""
        rows = connection.execute(
            "SELECT run_id, node_id, MAX(event_sequence) AS last_sequence, "
            f"SUM(size_bytes) AS size_bytes FROM {_TABLE} GROUP BY run_id, node_id"
        ).fetchall()
        return tuple(
            NodeEventTotals(**row_model(_TotalsRow, row, table=_TABLE).model_dump())
            for row in rows
        )

    @staticmethod
    def delete_run(connection: sqlite3.Connection, run_id: str) -> int:
        """Remove a run's classifier snapshots; return how many were removed."""
        return connection.execute(f"DELETE FROM {_TABLE} WHERE run_id = ?", (run_id,)).rowcount


def _event(row: sqlite3.Row) -> ClassifierEvent:
    stored = row_model(_ClassifierEventRow, row, table=_TABLE)
    return ClassifierEvent(
        invocation_id=stored.invocation_id,
        invocation_index=stored.invocation_index,
        answers=json_value(_ANSWERS, stored.answers_json, table=_TABLE, column="answers_json"),
        event_sequence=stored.event_sequence,
        event_json=stored.event_json,
        size_bytes=stored.size_bytes,
        event_kind=stored.event_kind,
        duration_ms=stored.duration_ms,
        error=stored.error,
    )
