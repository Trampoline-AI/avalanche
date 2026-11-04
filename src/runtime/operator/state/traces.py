"""Adapter for ``run_traces``, ``run_trace_bodies`` and ``run_trace_revisions``.

``run_traces`` holds each agent step's current :class:`TraceDescriptor` (JSON)
and its last error. A finalized trace body is stored once, in
``run_trace_bodies``, under the revision that published it. Every published
trace revision has a ``run_trace_revisions`` row naming the body it serves, so a
later revision that finalized nothing new serves the earlier body without
storing it again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt

from avalanche.agent.native import TraceHeaderNative
from avalanche.agent.trace import AgentTraceStatus, TokenUsage

from ..models import TraceDescriptor, TraceHeader
from .rows import json_column, row_model

_TRACES = "run_traces"
_BODIES = "run_trace_bodies"
_REVISIONS = "run_trace_revisions"


@dataclass(frozen=True)
class StoredTrace:
    """One step's current trace descriptor and its last error."""

    run_id: str
    node_id: str
    descriptor: TraceDescriptor
    error: str | None


@dataclass(frozen=True)
class TraceBody:
    """One finalized trace body and the revision it is served at."""

    revision: int
    data: bytes


@dataclass(frozen=True)
class TraceBodyTotals:
    """The bytes one step's stored trace bodies use."""

    run_id: str
    node_id: str
    size_bytes: int


class _StoredHeader(BaseModel):
    """The JSON form of a :class:`TraceHeader`, with its field types resolved."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: AgentTraceStatus
    model: str | None
    duration_ms: int
    usage: TokenUsage | None
    turn_count: int
    tool_call_count: int
    external_run_id: str | None
    stop_reason: str | None
    native: TraceHeaderNative


class _StoredDescriptor(BaseModel):
    """The JSON form of a :class:`TraceDescriptor`."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: str
    revision: NonNegativeInt
    available: bool
    complete: bool
    event_count: NonNegativeInt
    size_bytes: NonNegativeInt
    latest_event_sequence: NonNegativeInt
    header: _StoredHeader | None

    @classmethod
    def of(cls, descriptor: TraceDescriptor) -> _StoredDescriptor:
        header = descriptor.header
        return cls(
            status=descriptor.status,
            revision=descriptor.revision,
            available=descriptor.available,
            complete=descriptor.complete,
            event_count=descriptor.event_count,
            size_bytes=descriptor.size_bytes,
            latest_event_sequence=descriptor.latest_event_sequence,
            header=None
            if header is None
            else _StoredHeader(
                status=header.status,
                model=header.model,
                duration_ms=header.duration_ms,
                usage=header.usage,
                turn_count=header.turn_count,
                tool_call_count=header.tool_call_count,
                external_run_id=header.external_run_id,
                stop_reason=header.stop_reason,
                native=header.native,
            ),
        )

    def descriptor(self) -> TraceDescriptor:
        header = self.header
        return TraceDescriptor(
            status=self.status,
            revision=self.revision,
            available=self.available,
            complete=self.complete,
            event_count=self.event_count,
            size_bytes=self.size_bytes,
            latest_event_sequence=self.latest_event_sequence,
            header=None
            if header is None
            else TraceHeader(
                status=header.status,
                model=header.model,
                duration_ms=header.duration_ms,
                usage=header.usage,
                turn_count=header.turn_count,
                tool_call_count=header.tool_call_count,
                external_run_id=header.external_run_id,
                stop_reason=header.stop_reason,
                native=header.native,
            ),
        )


class _TraceRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    node_id: str
    descriptor_json: str
    error: str | None


class _BodyRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    revision: PositiveInt
    body: bytes


class _BodyTotalsRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    node_id: str
    size_bytes: NonNegativeInt


class RunTracesTable:
    """Read and write trace descriptors and bodies inside a store transaction."""

    @staticmethod
    def write_descriptor(
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
        descriptor: TraceDescriptor,
        error: str | None,
    ) -> None:
        connection.execute(
            f"INSERT INTO {_TRACES} (run_id, node_id, descriptor_json, error) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (run_id, node_id) DO UPDATE SET "
            "descriptor_json = excluded.descriptor_json, error = excluded.error",
            (run_id, node_id, _StoredDescriptor.of(descriptor).model_dump_json(), error),
        )

    @staticmethod
    def insert_body(
        connection: sqlite3.Connection, run_id: str, node_id: str, revision: int, body: bytes
    ) -> None:
        """Store a newly finalized body, served at the revision that published it."""
        connection.execute(
            f"INSERT INTO {_BODIES} (run_id, node_id, revision, body) VALUES (?, ?, ?, ?)",
            (run_id, node_id, revision, body),
        )
        connection.execute(
            f"INSERT INTO {_REVISIONS} (run_id, node_id, revision, body_revision) "
            "VALUES (?, ?, ?, ?)",
            (run_id, node_id, revision, revision),
        )

    @staticmethod
    def carry_body(
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
        revision: int,
        from_revision: int,
    ) -> None:
        """Serve the body of ``from_revision`` at ``revision`` too, if it has one."""
        connection.execute(
            f"INSERT INTO {_REVISIONS} (run_id, node_id, revision, body_revision) "
            f"SELECT run_id, node_id, ?, body_revision FROM {_REVISIONS} "
            "WHERE run_id = ? AND node_id = ? AND revision = ?",
            (revision, run_id, node_id, from_revision),
        )

    @staticmethod
    def read_body(
        connection: sqlite3.Connection, run_id: str, node_id: str, revision: int | None
    ) -> TraceBody | None:
        """The body served at ``revision``, or at the latest revision when ``None``."""
        revision_filter = "" if revision is None else " AND r.revision = ?"
        row = connection.execute(
            f"SELECT r.revision AS revision, b.body AS body FROM {_REVISIONS} AS r "
            f"JOIN {_BODIES} AS b ON b.run_id = r.run_id AND b.node_id = r.node_id "
            "AND b.revision = r.body_revision "
            f"WHERE r.run_id = ? AND r.node_id = ?{revision_filter} "
            "ORDER BY r.revision DESC LIMIT 1",
            (run_id, node_id, *(() if revision is None else (revision,))),
        ).fetchone()
        if row is None:
            return None
        stored = row_model(_BodyRow, row, table=_BODIES)
        return TraceBody(revision=stored.revision, data=stored.body)

    @staticmethod
    def load_descriptors(connection: sqlite3.Connection) -> tuple[StoredTrace, ...]:
        rows = connection.execute(
            f"SELECT run_id, node_id, descriptor_json, error FROM {_TRACES} "
            "ORDER BY run_id, node_id"
        ).fetchall()
        traces = []
        for row in rows:
            stored = row_model(_TraceRow, row, table=_TRACES)
            traces.append(
                StoredTrace(
                    run_id=stored.run_id,
                    node_id=stored.node_id,
                    descriptor=json_column(
                        _StoredDescriptor,
                        stored.descriptor_json,
                        table=_TRACES,
                        column="descriptor_json",
                    ).descriptor(),
                    error=stored.error,
                )
            )
        return tuple(traces)

    @staticmethod
    def body_totals(connection: sqlite3.Connection) -> tuple[TraceBodyTotals, ...]:
        """The bytes every step's stored trace bodies use."""
        rows = connection.execute(
            "SELECT run_id, node_id, SUM(LENGTH(body)) AS size_bytes "
            f"FROM {_BODIES} GROUP BY run_id, node_id"
        ).fetchall()
        return tuple(
            TraceBodyTotals(**row_model(_BodyTotalsRow, row, table=_BODIES).model_dump())
            for row in rows
        )

    @staticmethod
    def delete_run(connection: sqlite3.Connection, run_id: str) -> int:
        """Remove a run's trace descriptors and bodies; return how many bodies."""
        connection.execute(f"DELETE FROM {_REVISIONS} WHERE run_id = ?", (run_id,))
        connection.execute(f"DELETE FROM {_TRACES} WHERE run_id = ?", (run_id,))
        return connection.execute(f"DELETE FROM {_BODIES} WHERE run_id = ?", (run_id,)).rowcount
