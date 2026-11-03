"""What a run view shows besides the run itself: logs, events, traces, evaluations.

Each kind has its own adapter. This module gathers the per-run and per-step
counters the operator keeps in memory, derived from the stored rows when it
starts, and the delete that removes all of one run's details at once.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .agent_events import RunAgentEventsTable
from .classifier_events import RunClassifierEventsTable
from .evaluations import RunEvaluationsTable
from .logs import RunLogsTable
from .traces import RunTracesTable, StoredTrace

NodeKey = tuple[str, str]


@dataclass(frozen=True)
class RunDetailTotals:
    """The stored details' counters: last sequences and byte sizes.

    Node detail bytes count agent event, classifier snapshot and trace bodies;
    run detail bytes count those and the run's log messages, as the operator's
    per-node and per-run size limits do.
    """

    last_log_sequences: Mapping[str, int]
    run_log_bytes: Mapping[str, int]
    last_agent_event_sequences: Mapping[NodeKey, int]
    last_classifier_event_sequences: Mapping[NodeKey, int]
    node_detail_bytes: Mapping[NodeKey, int]
    run_detail_bytes: Mapping[str, int]
    traces: tuple[StoredTrace, ...]


@dataclass(frozen=True)
class DeletedRunDetails:
    """How many of each kind of detail a delete removed."""

    logs: int
    agent_events: int
    classifier_events: int
    trace_bodies: int
    evaluations: int


def load_run_detail_totals(connection: sqlite3.Connection) -> RunDetailTotals:
    """Derive every stored run's detail counters from its rows."""
    last_log_sequences = {}
    run_log_bytes = {}
    run_detail_bytes: dict[str, int] = {}
    for logs in RunLogsTable.totals(connection):
        last_log_sequences[logs.run_id] = logs.last_sequence
        run_log_bytes[logs.run_id] = logs.size_bytes
        run_detail_bytes[logs.run_id] = logs.size_bytes
    node_detail_bytes: dict[NodeKey, int] = {}

    def add_node_bytes(run_id: str, node_id: str, size_bytes: int) -> None:
        key = (run_id, node_id)
        node_detail_bytes[key] = node_detail_bytes.get(key, 0) + size_bytes
        run_detail_bytes[run_id] = run_detail_bytes.get(run_id, 0) + size_bytes

    last_agent_event_sequences = {}
    for events in RunAgentEventsTable.totals(connection):
        last_agent_event_sequences[(events.run_id, events.node_id)] = events.last_sequence
        add_node_bytes(events.run_id, events.node_id, events.size_bytes)
    last_classifier_event_sequences = {}
    for events in RunClassifierEventsTable.totals(connection):
        last_classifier_event_sequences[(events.run_id, events.node_id)] = events.last_sequence
        add_node_bytes(events.run_id, events.node_id, events.size_bytes)
    for bodies in RunTracesTable.body_totals(connection):
        add_node_bytes(bodies.run_id, bodies.node_id, bodies.size_bytes)
    return RunDetailTotals(
        last_log_sequences=MappingProxyType(last_log_sequences),
        run_log_bytes=MappingProxyType(run_log_bytes),
        last_agent_event_sequences=MappingProxyType(last_agent_event_sequences),
        last_classifier_event_sequences=MappingProxyType(last_classifier_event_sequences),
        node_detail_bytes=MappingProxyType(node_detail_bytes),
        run_detail_bytes=MappingProxyType(run_detail_bytes),
        traces=RunTracesTable.load_descriptors(connection),
    )


def delete_run_details(connection: sqlite3.Connection, run_id: str) -> DeletedRunDetails:
    """Remove all of one run's logs, events, traces and evaluations.

    Deleting the run's ``runs`` row removes them too; this lets a caller clear a
    run's details on their own, in the same transaction as other changes.
    """
    return DeletedRunDetails(
        logs=RunLogsTable.delete_run(connection, run_id),
        agent_events=RunAgentEventsTable.delete_run(connection, run_id),
        classifier_events=RunClassifierEventsTable.delete_run(connection, run_id),
        trace_bodies=RunTracesTable.delete_run(connection, run_id),
        evaluations=RunEvaluationsTable.delete_run(connection, run_id),
    )
