"""Adapter for ``runs``, ``run_nodes`` and ``run_input_files``: run history.

A run row holds the structural :class:`RunState` (status, timestamps, topology,
validated input and terminal seal), the revision counters clients compare, the
request the run was started with, and the coordinator process group recorded
when it started. Its nodes are rows of ``run_nodes`` in topology order, and the
staged files its input used are rows of ``run_input_files``. Logs, agent and
classifier events, traces, evaluations and results are stored by their own
adapters.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
)

from ..input_files import StagedInputFile
from ..models import (
    NodeState,
    NodeStatus,
    RunInput,
    RunState,
    RunStatus,
    TerminalSealDescriptor,
    TriggerKind,
    WorkflowTopology,
)
from ..run_processes import RunProcess
from ..step_io import StepIo
from .errors import StateRowError
from .rows import json_column, json_value, row_model

_RUNS = "runs"
_NODES = "run_nodes"
_INPUT_FILES = "run_input_files"
_TOPOLOGY = TypeAdapter(WorkflowTopology)
_RUN_INPUT = TypeAdapter(RunInput)
_TERMINAL_SEAL = TypeAdapter(TerminalSealDescriptor)


class RunRequest(BaseModel):
    """What a run was started with, kept so a later rerun can start it again.

    ``input`` and ``context`` are the caller's values in JSON form; an input file
    appears in ``input`` as its ``ava.File`` reference. ``input_files`` are the
    staged files the input used, in attachment order.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    input: dict[str, JsonValue] | None = None
    context: dict[str, JsonValue] | None = None
    input_files: tuple[StagedInputFile, ...] = ()


@dataclass(frozen=True)
class StoredRun:
    """One run as stored: its state and the revision counters published with it."""

    run: RunState
    created_sequence: int
    revision: int
    activity_sequence: int
    node_revisions: Mapping[str, int]


@dataclass(frozen=True)
class LoadedRun:
    """A stored run read back at startup, with the process group it recorded."""

    stored: StoredRun
    process: RunProcess | None


class _RequestValues(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    input: dict[str, JsonValue] | None
    context: dict[str, JsonValue] | None


class _RunRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    flow_name: str
    status: RunStatus = Field(strict=False)
    triggered_at: float | None
    started_at: float | None
    ended_at: float | None
    triggered_by: TriggerKind = Field(strict=False)
    workflow_id: str
    workflow_display_name: str
    topology_json: str
    input_json: str | None
    terminal_seal_json: str | None
    created_sequence: PositiveInt
    revision: PositiveInt
    activity_sequence: NonNegativeInt
    process_group: PositiveInt | None
    process_started: str | None


class _NodeRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    node_id: str
    name: str
    node_type: str
    status: NodeStatus = Field(strict=False)
    started_at: float | None
    ended_at: float | None
    error: str | None
    step_io_json: str | None
    revision: PositiveInt


class _InputFileRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    object_key: str
    name: str
    media_type: str
    sha256: str
    size_bytes: NonNegativeInt


# Columns that change while a run is published; the request and process are fixed.
_STATE_COLUMNS = (
    "flow_name",
    "status",
    "triggered_at",
    "started_at",
    "ended_at",
    "triggered_by",
    "workflow_id",
    "workflow_display_name",
    "topology_json",
    "input_json",
    "terminal_seal_json",
    "created_sequence",
    "revision",
    "activity_sequence",
)
_START_COLUMNS = ("request_json", "process_group", "process_started")
_NODE_COLUMNS = (
    "node_id",
    "position",
    "name",
    "node_type",
    "status",
    "started_at",
    "ended_at",
    "error",
    "step_io_json",
    "revision",
)
_INSERT_RUN = (
    f"INSERT INTO {_RUNS} (run_id, {', '.join(_STATE_COLUMNS + _START_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in range(1 + len(_STATE_COLUMNS) + len(_START_COLUMNS)))})"
)
_UPDATE_RUN = (
    f"UPDATE {_RUNS} SET {', '.join(f'{column} = ?' for column in _STATE_COLUMNS)} "
    "WHERE run_id = ?"
)
_UPSERT_NODE = (
    f"INSERT INTO {_NODES} (run_id, {', '.join(_NODE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in range(1 + len(_NODE_COLUMNS)))}) "
    "ON CONFLICT (run_id, node_id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in _NODE_COLUMNS[1:])
)


class RunsTable:
    """Read and write runs, their nodes and their input files in a store transaction."""

    @staticmethod
    def create(
        connection: sqlite3.Connection,
        stored: StoredRun,
        request: RunRequest,
        process: RunProcess | None,
    ) -> None:
        """Insert a new run with its nodes, request, input files and process group."""
        connection.execute(
            _INSERT_RUN,
            (
                stored.run.run_id,
                *_state_values(stored),
                _RequestValues(input=request.input, context=request.context).model_dump_json(),
                None if process is None else process.process_group,
                None if process is None else process.started,
            ),
        )
        connection.executemany(
            f"INSERT INTO {_INPUT_FILES} "
            "(run_id, position, object_key, name, media_type, sha256, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    stored.run.run_id,
                    position,
                    file.object_key,
                    file.name,
                    file.media_type,
                    file.sha256,
                    file.size_bytes,
                )
                for position, file in enumerate(request.input_files)
            ),
        )
        _upsert_nodes(connection, stored, stored.run.nodes)

    @staticmethod
    def update(
        connection: sqlite3.Connection, stored: StoredRun, node_ids: Collection[str]
    ) -> None:
        """Update a stored run, and insert or update the rows of the given nodes."""
        cursor = connection.execute(_UPDATE_RUN, (*_state_values(stored), stored.run.run_id))
        if cursor.rowcount != 1:
            raise StateRowError(f"Run {stored.run.run_id} is not stored")
        _upsert_nodes(connection, stored, node_ids)

    @staticmethod
    def delete(connection: sqlite3.Connection, run_id: str) -> None:
        """Remove a run with its nodes and input files."""
        connection.execute(f"DELETE FROM {_RUNS} WHERE run_id = ?", (run_id,))

    @staticmethod
    def load_all(connection: sqlite3.Connection) -> tuple[LoadedRun, ...]:
        """Every stored run with its nodes, in creation order."""
        nodes: dict[str, list[_NodeRow]] = {}
        for row in connection.execute(
            "SELECT run_id, node_id, name, node_type, status, started_at, ended_at, error, "
            f"step_io_json, revision FROM {_NODES} ORDER BY run_id, position"
        ):
            node = row_model(_NodeRow, row, table=_NODES)
            nodes.setdefault(node.run_id, []).append(node)
        runs = []
        for row in connection.execute(
            f"SELECT run_id, {', '.join(_STATE_COLUMNS)}, process_group, process_started "
            f"FROM {_RUNS} ORDER BY created_sequence, run_id"
        ):
            run_row = row_model(_RunRow, row, table=_RUNS)
            runs.append(_loaded_run(run_row, nodes.pop(run_row.run_id, ())))
        return tuple(runs)

    @staticmethod
    def read_request(connection: sqlite3.Connection, run_id: str) -> RunRequest:
        """The request a stored run was started with."""
        row = connection.execute(
            f"SELECT request_json FROM {_RUNS} WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        values = json_column(
            _RequestValues, row["request_json"], table=_RUNS, column="request_json"
        )
        files = tuple(
            StagedInputFile(
                **row_model(_InputFileRow, file_row, table=_INPUT_FILES).model_dump()
            )
            for file_row in connection.execute(
                "SELECT object_key, name, media_type, sha256, size_bytes "
                f"FROM {_INPUT_FILES} WHERE run_id = ? ORDER BY position",
                (run_id,),
            )
        )
        return RunRequest(input=values.input, context=values.context, input_files=files)


def _state_values(stored: StoredRun) -> tuple[str | float | int | None, ...]:
    run = stored.run
    return (
        run.flow_name,
        run.status.value,
        run.triggered_at,
        run.started_at,
        run.ended_at,
        run.triggered_by.value,
        run.workflow_id,
        run.workflow_display_name,
        _TOPOLOGY.dump_json(run.topology).decode(),
        None if run.input is None else _RUN_INPUT.dump_json(run.input).decode(),
        None
        if run.terminal_seal is None
        else _TERMINAL_SEAL.dump_json(run.terminal_seal).decode(),
        stored.created_sequence,
        stored.revision,
        stored.activity_sequence,
    )


def _upsert_nodes(
    connection: sqlite3.Connection, stored: StoredRun, node_ids: Collection[str]
) -> None:
    connection.executemany(_UPSERT_NODE, _node_values(stored, node_ids))


def _node_values(
    stored: StoredRun, node_ids: Collection[str]
) -> Iterator[tuple[str | float | int | None, ...]]:
    for position, node in enumerate(stored.run.nodes.values()):
        if node.node_id not in node_ids:
            continue
        yield (
            stored.run.run_id,
            node.node_id,
            position,
            node.name,
            node.node_type,
            node.status.value,
            node.started_at,
            node.ended_at,
            node.error,
            None if node.step_io is None else node.step_io.model_dump_json(),
            stored.node_revisions[node.node_id],
        )


def _loaded_run(row: _RunRow, node_rows: Collection[_NodeRow]) -> LoadedRun:
    run = RunState(
        run_id=row.run_id,
        flow_name=row.flow_name,
        status=row.status,
        triggered_at=row.triggered_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        triggered_by=row.triggered_by,
        workflow_id=row.workflow_id,
        workflow_display_name=row.workflow_display_name,
        topology=json_value(_TOPOLOGY, row.topology_json, table=_RUNS, column="topology_json"),
        input=None
        if row.input_json is None
        else json_value(_RUN_INPUT, row.input_json, table=_RUNS, column="input_json"),
        terminal_seal=None
        if row.terminal_seal_json is None
        else json_value(
            _TERMINAL_SEAL, row.terminal_seal_json, table=_RUNS, column="terminal_seal_json"
        ),
    )
    node_revisions = {}
    for node in node_rows:
        run.nodes[node.node_id] = NodeState(
            node_id=node.node_id,
            name=node.name,
            node_type=node.node_type,
            status=node.status,
            started_at=node.started_at,
            ended_at=node.ended_at,
            error=node.error,
            step_io=None
            if node.step_io_json is None
            else json_column(StepIo, node.step_io_json, table=_NODES, column="step_io_json"),
        )
        node_revisions[node.node_id] = node.revision
    process = None
    if row.process_group is not None and row.process_started is not None:
        process = RunProcess(process_group=row.process_group, started=row.process_started)
    return LoadedRun(
        stored=StoredRun(
            run=run,
            created_sequence=row.created_sequence,
            revision=row.revision,
            activity_sequence=row.activity_sequence,
            node_revisions=MappingProxyType(node_revisions),
        ),
        process=process,
    )
