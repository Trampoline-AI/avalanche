"""Adapter for ``run_agent_events``: each agent step's live events.

A row is an :class:`AgentEvent`: its sequence among the step's events, the
invocation it belongs to, its descriptor fields and its verbatim JSON body. Rows
are written in the publication that announces them and read back for paging,
body reads and the full trace view.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from ..models import AgentEvent, AgentEventKind
from .rows import row_model

_TABLE = "run_agent_events"
_COLUMNS = (
    "event_sequence, invocation_id, event_json, size_bytes, native_event_kind, "
    "agent_event_kind, turn, duration_ms, error, tool_call_count"
)


@dataclass(frozen=True)
class NodeEventTotals:
    """One step's last event sequence and the bytes its event bodies use."""

    run_id: str
    node_id: str
    last_sequence: int
    size_bytes: int


class _AgentEventRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    event_sequence: PositiveInt
    invocation_id: str
    event_json: str
    size_bytes: NonNegativeInt
    native_event_kind: str
    agent_event_kind: AgentEventKind
    turn: int | None
    duration_ms: int | None
    # SQLite has no boolean type; the column holds 0 or 1.
    error: bool = Field(strict=False)
    tool_call_count: NonNegativeInt


class _TotalsRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    node_id: str
    last_sequence: PositiveInt
    size_bytes: NonNegativeInt


class RunAgentEventsTable:
    """Read and write ``run_agent_events`` rows inside a store transaction."""

    @staticmethod
    def insert(
        connection: sqlite3.Connection, run_id: str, node_id: str, event: AgentEvent
    ) -> None:
        connection.execute(
            f"INSERT INTO {_TABLE} (run_id, node_id, {_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                node_id,
                event.event_sequence,
                event.invocation_id,
                event.event_json,
                event.size_bytes,
                event.native_event_kind,
                event.agent_event_kind,
                event.turn,
                event.duration_ms,
                int(event.error),
                event.tool_call_count,
            ),
        )

    @staticmethod
    def get(
        connection: sqlite3.Connection, run_id: str, node_id: str, event_sequence: int
    ) -> AgentEvent | None:
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
    ) -> tuple[AgentEvent, ...]:
        """Up to ``limit`` events with ``after < event_sequence <= through``."""
        order = "DESC" if newest_first else "ASC"
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} "
            "WHERE run_id = ? AND node_id = ? AND event_sequence > ? AND event_sequence <= ? "
            f"ORDER BY event_sequence {order} LIMIT ?",
            (run_id, node_id, after, through, limit),
        ).fetchall()
        return tuple(_event(row) for row in rows)

    @staticmethod
    def list_node(
        connection: sqlite3.Connection, run_id: str, node_id: str
    ) -> tuple[AgentEvent, ...]:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE run_id = ? AND node_id = ? "
            "ORDER BY event_sequence",
            (run_id, node_id),
        ).fetchall()
        return tuple(_event(row) for row in rows)

    @staticmethod
    def totals(connection: sqlite3.Connection) -> tuple[NodeEventTotals, ...]:
        """Every step's last event sequence and event size, from its stored rows."""
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
        """Remove a run's agent events; return how many were removed."""
        return connection.execute(f"DELETE FROM {_TABLE} WHERE run_id = ?", (run_id,)).rowcount


def _event(row: sqlite3.Row) -> AgentEvent:
    return AgentEvent(**row_model(_AgentEventRow, row, table=_TABLE).model_dump())
