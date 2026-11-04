"""Adapter for ``run_logs``: each run's log, one row per entry in append order.

A row is a :class:`SequencedLogEntry`: the run's log sequence, the entry's
fields as columns and its size in bytes. The operator writes a row in the same
transaction as the publication that announces it, and pages and reads bodies
from here, so a run's log after a restart is the one clients saw before it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from ..models import LogEntry, LogLevel, SequencedLogEntry
from .rows import row_model

_TABLE = "run_logs"
_COLUMNS = "sequence, node_id, timestamp, level, channel, attempt, message, size_bytes"


@dataclass(frozen=True)
class LogTotals:
    """One run's last log sequence and the bytes its messages use."""

    run_id: str
    last_sequence: int
    size_bytes: int


class _LogRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sequence: PositiveInt
    node_id: str
    # Stored as ISO 8601 text, the only form SQLite keeps without loss.
    timestamp: datetime = Field(strict=False)
    level: LogLevel = Field(strict=False)
    channel: str | None
    attempt: str | None
    message: str
    size_bytes: NonNegativeInt


class _TotalsRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    last_sequence: PositiveInt
    size_bytes: NonNegativeInt


class RunLogsTable:
    """Read and write ``run_logs`` rows inside a store transaction."""

    @staticmethod
    def insert(connection: sqlite3.Connection, run_id: str, item: SequencedLogEntry) -> None:
        entry = item.entry
        connection.execute(
            f"INSERT INTO {_TABLE} (run_id, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                item.sequence,
                entry.node_id,
                entry.timestamp.isoformat(),
                entry.level.value,
                entry.channel,
                entry.attempt,
                entry.message,
                item.size_bytes,
            ),
        )

    @staticmethod
    def get(
        connection: sqlite3.Connection, run_id: str, sequence: int
    ) -> SequencedLogEntry | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE run_id = ? AND sequence = ?",
            (run_id, sequence),
        ).fetchone()
        return None if row is None else _entry(row)

    @staticmethod
    def page(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        after: int,
        through: int,
        limit: int,
        node_id: str = "",
        newest_first: bool = False,
    ) -> tuple[SequencedLogEntry, ...]:
        """Up to ``limit`` entries with ``after < sequence <= through``, optionally of
        one node, oldest first or newest first."""
        node_filter = " AND node_id = ?" if node_id else ""
        order = "DESC" if newest_first else "ASC"
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            f"WHERE run_id = ? AND sequence > ? AND sequence <= ?{node_filter} "
            f"ORDER BY sequence {order} LIMIT ?",
            (run_id, after, through, *((node_id,) if node_id else ()), limit),
        ).fetchall()
        return tuple(_entry(row) for row in rows)

    @staticmethod
    def list_run(connection: sqlite3.Connection, run_id: str) -> tuple[SequencedLogEntry, ...]:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        return tuple(_entry(row) for row in rows)

    @staticmethod
    def totals(connection: sqlite3.Connection) -> tuple[LogTotals, ...]:
        """Every run's last log sequence and log size, from its stored rows."""
        rows = connection.execute(
            "SELECT run_id, MAX(sequence) AS last_sequence, SUM(size_bytes) AS size_bytes "
            f"FROM {_TABLE} GROUP BY run_id"
        ).fetchall()
        return tuple(
            LogTotals(**row_model(_TotalsRow, row, table=_TABLE).model_dump()) for row in rows
        )

    @staticmethod
    def delete_run(connection: sqlite3.Connection, run_id: str) -> int:
        """Remove a run's log; return how many entries were removed."""
        return connection.execute(f"DELETE FROM {_TABLE} WHERE run_id = ?", (run_id,)).rowcount


def _entry(row: sqlite3.Row) -> SequencedLogEntry:
    stored = row_model(_LogRow, row, table=_TABLE)
    return SequencedLogEntry(
        sequence=stored.sequence,
        entry=LogEntry(
            timestamp=stored.timestamp,
            level=stored.level,
            node_id=stored.node_id,
            message=stored.message,
            channel=stored.channel,
            attempt=stored.attempt,
        ),
        size_bytes=stored.size_bytes,
    )
