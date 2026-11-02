"""Adapter for ``schedules``: each workflow schedule's enabled state, input and last run.

Rows are keyed by canonical workflow ID, never by cron expression, so a schedule
whose cron changes in source keeps its state, and a row whose workflow is no
longer discovered stays until the workflow comes back.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from avalanche.file import WorkspaceFileRef

from ..models import ScheduleInputAdapter
from .errors import StateRowError
from .rows import json_column, row_model
from .schedule_input_files import (
    ScheduleInputFile,
    ScheduleInputFileFolder,
    ScheduleInputFiles,
)
from .store import OperatorStateStore

_TABLE = "schedules"


@dataclass(frozen=True)
class PersistedSchedule:
    """One workflow's stored schedule state.

    ``enabled`` is None until someone turns the schedule on or off, so the
    operator's startup default applies. ``input`` is None while runs use the
    workflow's defaults. ``input_files`` are the uploaded files the input refers
    to, kept in the schedule input folder next to the database.
    """

    workflow_id: str
    enabled: bool | None
    enabled_minute: float | None
    input: dict[str, JsonValue] | None
    input_files: tuple[ScheduleInputFile, ...]
    last_triggered: float | None


class _ScheduleRow(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    workflow_id: str
    enabled: Literal[0, 1] | None
    enabled_minute: float | None
    input_json: str | None
    input_files_json: str
    last_triggered: float | None


class SchedulesTable:
    """Read and write ``schedules`` rows inside a store transaction."""

    @staticmethod
    def read_all(connection: sqlite3.Connection) -> tuple[PersistedSchedule, ...]:
        rows = connection.execute(
            "SELECT workflow_id, enabled, enabled_minute, input_json, input_files_json, "
            "last_triggered "
            f"FROM {_TABLE} ORDER BY workflow_id"
        ).fetchall()
        return tuple(_schedule(row_model(_ScheduleRow, row, table=_TABLE)) for row in rows)

    @staticmethod
    def read(connection: sqlite3.Connection, workflow_id: str) -> PersistedSchedule | None:
        row = connection.execute(
            "SELECT workflow_id, enabled, enabled_minute, input_json, input_files_json, "
            "last_triggered "
            f"FROM {_TABLE} WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        return None if row is None else _schedule(row_model(_ScheduleRow, row, table=_TABLE))

    @staticmethod
    def write_enabled(
        connection: sqlite3.Connection,
        workflow_id: str,
        enabled: bool,
        *,
        enabled_minute: float | None,
    ) -> None:
        """Store an on or off choice; ``enabled_minute`` None keeps the stored minute."""
        connection.execute(
            f"INSERT INTO {_TABLE} (workflow_id, enabled, enabled_minute, input_files_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (workflow_id) DO UPDATE SET "
            "enabled = excluded.enabled, "
            "enabled_minute = COALESCE(excluded.enabled_minute, enabled_minute)",
            (workflow_id, int(enabled), enabled_minute, _NO_FILES),
        )

    @staticmethod
    def write_input(
        connection: sqlite3.Connection,
        workflow_id: str,
        *,
        enabled: bool,
        enabled_minute: float | None,
        run_input: dict[str, JsonValue],
        input_files: tuple[ScheduleInputFile, ...],
    ) -> None:
        """Store a choice with new input; ``enabled_minute`` None keeps the stored minute."""
        connection.execute(
            f"INSERT INTO {_TABLE} (workflow_id, enabled, enabled_minute, input_json, "
            "input_files_json) VALUES (?, ?, ?, ?, ?) ON CONFLICT (workflow_id) DO UPDATE "
            "SET enabled = excluded.enabled, "
            "enabled_minute = COALESCE(excluded.enabled_minute, enabled_minute), "
            "input_json = excluded.input_json, input_files_json = excluded.input_files_json",
            (
                workflow_id,
                int(enabled),
                enabled_minute,
                ScheduleInputAdapter.dump_json(run_input).decode(),
                ScheduleInputFiles(files=input_files).model_dump_json(),
            ),
        )

    @staticmethod
    def write_last_triggered(
        connection: sqlite3.Connection, workflow_id: str, last_triggered: float
    ) -> None:
        connection.execute(
            f"INSERT INTO {_TABLE} (workflow_id, input_files_json, last_triggered) "
            "VALUES (?, ?, ?) ON CONFLICT (workflow_id) DO UPDATE SET "
            "last_triggered = excluded.last_triggered",
            (workflow_id, _NO_FILES, last_triggered),
        )


_NO_FILES = ScheduleInputFiles(files=()).model_dump_json()


def _schedule(row: _ScheduleRow) -> PersistedSchedule:
    run_input = None
    if row.input_json is not None:
        try:
            run_input = ScheduleInputAdapter.validate_json(row.input_json)
        except ValidationError as exc:
            raise StateRowError(
                f"Invalid JSON in operator state column {_TABLE}.input_json: {exc}"
            ) from exc
    files = json_column(
        ScheduleInputFiles, row.input_files_json, table=_TABLE, column="input_files_json"
    )
    return PersistedSchedule(
        workflow_id=row.workflow_id,
        enabled=None if row.enabled is None else row.enabled == 1,
        enabled_minute=row.enabled_minute,
        input=run_input,
        input_files=files.files,
        last_triggered=row.last_triggered,
    )


class ScheduleState:
    """Keep schedule state in the store and the files schedule inputs use in their folder.

    The scheduler calls these methods while holding its own locks; the store lock
    is always taken last.
    """

    def __init__(self, store: OperatorStateStore, files: ScheduleInputFileFolder) -> None:
        self._store = store
        self._files = files

    @property
    def files(self) -> ScheduleInputFileFolder:
        return self._files

    def load(self) -> tuple[PersistedSchedule, ...]:
        """Read every stored schedule and register its input files for this host again."""
        with self._store.transaction() as connection:
            schedules = SchedulesTable.read_all(connection)
        for schedule in schedules:
            for input_file in schedule.input_files:
                self._files.restore(schedule.workflow_id, input_file)
        return schedules

    def save_enabled(
        self, workflow_id: str, enabled: bool, *, enabled_minute: float | None
    ) -> None:
        """Store an on or off choice, keeping the schedule's input.

        ``enabled_minute`` is the minute an off schedule is turned on in, else None.
        """
        with self._store.transaction() as connection:
            SchedulesTable.write_enabled(
                connection, workflow_id, enabled, enabled_minute=enabled_minute
            )

    def save_input(
        self,
        workflow_id: str,
        *,
        enabled: bool,
        enabled_minute: float | None,
        run_input: dict[str, JsonValue],
        input_files: tuple[ScheduleInputFile, ...],
    ) -> None:
        """Replace the schedule's input; files the new input no longer uses are removed.

        ``input_files`` are the files just copied for this input. Files the previous
        input owned stay when the new input still refers to them, as when a client
        sends the stored input back with one field changed.
        """
        referenced = _file_workspaces(run_input)
        with self._store.transaction() as connection:
            previous = SchedulesTable.read(connection, workflow_id)
            previous_files = () if previous is None else previous.input_files
            kept = tuple(
                item for item in previous_files if item.workspace_reference in referenced
            )
            SchedulesTable.write_input(
                connection,
                workflow_id,
                enabled=enabled,
                enabled_minute=enabled_minute,
                run_input=run_input,
                input_files=(*kept, *input_files),
            )
        # The new input is committed; files only the old input used are garbage now.
        self._files.remove(item for item in previous_files if item not in kept)

    def save_last_triggered(self, workflow_id: str, last_triggered: float) -> None:
        """Store the minute a schedule last started a run."""
        with self._store.transaction() as connection:
            SchedulesTable.write_last_triggered(connection, workflow_id, last_triggered)


class _FileReference(BaseModel):
    """The part of an ``ava.File`` wire value that names its workspace."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["file"]
    ref: WorkspaceFileRef


def _file_workspaces(value: JsonValue) -> set[str]:
    """Workspace references of every ``ava.File`` wire value inside schedule input."""
    if isinstance(value, list):
        return {reference for item in value for reference in _file_workspaces(item)}
    if not isinstance(value, dict):
        return set()
    if value.get("kind") == "file":
        try:
            return {_FileReference.model_validate(value).ref.workspace}
        except ValidationError:
            # Input data that merely has a "kind" key, not a file reference.
            pass
    return {reference for item in value.values() for reference in _file_workspaces(item)}
