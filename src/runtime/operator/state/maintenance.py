"""Inspect and clear what an operator state database keeps: the ``ava state`` commands.

A :class:`StateMaintenance` holds the database lock and the result-storage lock
for as long as it is open, so it fails at once while an operator uses the
database, and no operator can start on it in the meantime.

Deleting a run is one transaction that removes its ``runs`` row (and, through
``ON DELETE CASCADE``, its steps, input-file records, logs, events, traces and
evaluations) together with its result index row. Its files, the result bundle and
the staged input-file folders no remaining run uses, are removed after that
commit. An interruption in between leaves only files nothing refers to:
:meth:`StateMaintenance.info` reports them and :meth:`StateMaintenance.clear`
removes them.

The ``results/``, ``input-files/`` and ``schedule-inputs/`` folders next to the
database belong to it alone, as the result storage lock already requires of a
running operator.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from ..input_files import INPUT_FILE_FOLDER_NAME
from ..models import RunStatus
from ..result_store import ResultIndexReport, ResultStore
from ..run_processes import DEFAULT_CANCEL_GRACE_SECONDS, stop_run_process
from .errors import StateRowError, StateStoreError
from .results import RunResultRow, RunResultTable
from .run_details import DeletedRunDetails, delete_run_details
from .runs import RunsTable, RunSummary
from .schedule_input_files import SCHEDULE_INPUT_FOLDER_NAME
from .schedules import SchedulesTable
from .store import MEMORY, OperatorStateStore, StateDbPath

UNFINISHED_STATUSES = frozenset(
    {RunStatus.REQUESTING, RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING}
)
"""Runs an operator stopped before they ended; the next operator start fails them."""

VACUUM_FREE_FRACTION = 0.25
"""Rebuild the database file once at least this share of its pages is free."""

_TOKEN = re.compile(r"[0-9a-f]{32}")


class StateMaintenanceError(StateStoreError):
    """An ``ava state`` request that cannot be carried out; nothing was changed."""


@dataclass(frozen=True)
class FolderUsage:
    """One folder next to the database: how many entries it keeps and their size."""

    path: Path
    entries: int
    size_bytes: int


@dataclass(frozen=True)
class StateInfo:
    """What a state database and the folders next to it keep."""

    database: Path
    database_bytes: int
    """The database file and its write-ahead log, as found when opened."""
    runs_by_status: Mapping[RunStatus, int]
    schedules: int
    results: FolderUsage
    """``entries`` counts stored results (index rows)."""
    input_files: FolderUsage
    """``entries`` counts staged input-file folders."""
    schedule_inputs: FolderUsage
    """``entries`` counts schedule input-file folders."""
    result_index: ResultIndexReport
    unreferenced_input_files: tuple[Path, ...]
    """Input-file folders no stored run uses."""
    unreferenced_schedule_inputs: tuple[Path, ...]
    """Schedule input-file folders no stored schedule uses."""
    unexpected_entries: tuple[Path, ...]
    """Entries in the input folders that the operator did not create."""
    removed_pending_bundles: int
    """Unfinished result bundles removed on open, as an operator start does."""

    @property
    def runs(self) -> int:
        return sum(self.runs_by_status.values())

    @property
    def unfinished_runs(self) -> int:
        return sum(self.runs_by_status.get(status, 0) for status in UNFINISHED_STATUSES)


@dataclass(frozen=True)
class DeletedRun:
    """One deleted run and what was removed with it."""

    run_id: str
    status: RunStatus
    details: DeletedRunDetails
    result_bundle: str | None
    """The run's result bundle, or ``None`` when it stored no result."""
    input_file_folders: tuple[str, ...]
    """Staged input-file folders it used that no remaining run uses."""


@dataclass(frozen=True)
class PrunedRuns:
    """The finished runs a prune deleted, and the unfinished runs it left alone."""

    deleted: tuple[DeletedRun, ...]
    unfinished_kept: int


@dataclass(frozen=True)
class StoppedRunProcess:
    """A process group of an unfinished run, still alive, that a clear stopped."""

    run_id: str
    process_group: int


@dataclass(frozen=True)
class ClearedState:
    """What a clear removed, and the schedules it kept."""

    runs: int
    stopped_processes: tuple[StoppedRunProcess, ...]
    result_bundles: int
    input_file_folders: int
    schedules_deleted: int
    schedules_kept: int
    schedule_input_folders: int


@dataclass(frozen=True)
class SpaceReclaimed:
    """How the database file changed after a deletion."""

    vacuumed: bool
    free_bytes: int
    """Free space inside the database before compacting."""
    bytes_before: int
    bytes_after: int


class StateMaintenance:
    """Hold an operator state database and its result storage for inspection or clearing."""

    def __init__(
        self,
        store: OperatorStateStore,
        database: Path,
        results: ResultStore,
        *,
        bytes_at_open: int,
    ) -> None:
        """Wrap an open store and result store; use :meth:`open`."""
        self._store = store
        self._database = database
        self._results = results
        self._input_root = database.parent / INPUT_FILE_FOLDER_NAME
        self._schedule_root = database.parent / SCHEDULE_INPUT_FOLDER_NAME
        self._bytes_at_open = bytes_at_open

    @classmethod
    def open(cls, path: StateDbPath) -> StateMaintenance:
        """Lock an existing database file and the result storage next to it.

        Raises :class:`~runtime.operator.state.StateStoreLockedError` while an
        operator holds the database.
        """
        if os.fspath(path) == MEMORY:
            raise StateMaintenanceError(
                "ava state needs a database file: a :memory: database is private to the "
                "operator that opened it."
            )
        database = Path(path).expanduser().resolve()
        if not database.is_file():
            raise StateMaintenanceError(
                f"No operator state database at {database}. Pass the --state-db the "
                "operator uses."
            )
        bytes_at_open = _database_bytes(database)
        store = OperatorStateStore.open(database)
        try:
            results = ResultStore(store)
        except (RuntimeError, ValueError, OSError) as exc:
            # The result store's documented failures: locked, unsupported platform,
            # a root that is not private, or a file system error.
            failure = StateMaintenanceError(
                f"Could not open the result storage next to {database}: {exc}"
            )
            _close_after(store, failure)
            raise failure from exc
        except BaseException as failure:
            _close_after(store, failure)
            raise
        return cls(store, database, results, bytes_at_open=bytes_at_open)

    def __enter__(self) -> StateMaintenance:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def database(self) -> Path:
        return self._database

    def info(self) -> StateInfo:
        """Count what the database keeps and find files nothing refers to."""
        with self._store.transaction() as connection:
            summaries = RunsTable.list_summaries(connection)
            used_keys = RunsTable.input_file_keys_in_use(connection)
            schedules = SchedulesTable.read_all(connection)
            stored_results = len(RunResultTable.list_all(connection))
        runs_by_status: dict[RunStatus, int] = {}
        for summary in summaries:
            runs_by_status[summary.status] = runs_by_status.get(summary.status, 0) + 1
        input_folders, unexpected_inputs = _token_folders(self._input_root)
        schedule_folders, unexpected_schedule_inputs = _token_folders(self._schedule_root)
        used_input_folders = {_token_folder(key) for key in used_keys}
        used_schedule_folders = {
            input_file.folder for schedule in schedules for input_file in schedule.input_files
        }
        return StateInfo(
            database=self._database,
            database_bytes=self._bytes_at_open,
            runs_by_status=MappingProxyType(runs_by_status),
            schedules=len(schedules),
            results=FolderUsage(
                self._results.root, stored_results, _tree_bytes(self._results.root)
            ),
            input_files=FolderUsage(
                self._input_root, len(input_folders), _tree_bytes(self._input_root)
            ),
            schedule_inputs=FolderUsage(
                self._schedule_root, len(schedule_folders), _tree_bytes(self._schedule_root)
            ),
            result_index=self._results.check_index(),
            unreferenced_input_files=tuple(
                self._input_root / name
                for name in input_folders
                if name not in used_input_folders
            ),
            unreferenced_schedule_inputs=tuple(
                self._schedule_root / name
                for name in schedule_folders
                if name not in used_schedule_folders
            ),
            unexpected_entries=(
                *(self._input_root / name for name in unexpected_inputs),
                *(self._schedule_root / name for name in unexpected_schedule_inputs),
            ),
            removed_pending_bundles=self._results.recovered_pending_bundles,
        )

    def delete(
        self, run_ids: Sequence[str], *, on_deleted: Callable[[DeletedRun], None]
    ) -> tuple[DeletedRun, ...]:
        """Delete finished runs with everything that belongs to them, one run at a time.

        Every ID is checked first: an unknown or unfinished run refuses the whole
        request before anything is deleted. ``on_deleted`` hears about each run as
        soon as it is deleted, so a failure on a later run never hides earlier ones.
        """
        requested = tuple(dict.fromkeys(run_ids))
        with self._store.transaction() as connection:
            stored = RunsTable.list_summaries(connection)
        summaries = {summary.run_id: summary for summary in stored}
        unknown = [run_id for run_id in requested if run_id not in summaries]
        if unknown:
            raise StateMaintenanceError(
                f"No stored run {', '.join(unknown)} in {self._database}; nothing was deleted."
            )
        unfinished = [
            f"{run_id} ({summaries[run_id].status.value})"
            for run_id in requested
            if summaries[run_id].status in UNFINISHED_STATUSES
        ]
        if unfinished:
            raise StateMaintenanceError(
                f"Run {', '.join(unfinished)} did not finish before its operator stopped, "
                "and its processes may still be running; nothing was deleted. Start the "
                "operator on this database once: it stops them and marks the run failed. "
                "Then delete it, or use `ava state clear`."
            )
        return self._delete_runs([summaries[run_id] for run_id in requested], on_deleted)

    def prune_before(
        self, cutoff: datetime, *, on_deleted: Callable[[DeletedRun], None]
    ) -> PrunedRuns:
        """Delete finished runs triggered before ``cutoff``, a time-zone aware datetime.

        See :meth:`delete` for ``on_deleted``.
        """
        if cutoff.tzinfo is None:
            raise ValueError("The prune cutoff needs a time zone")
        timestamp = cutoff.timestamp()
        return self._prune(
            lambda finished: [
                summary
                for summary in finished
                if summary.triggered_at is not None and summary.triggered_at < timestamp
            ],
            on_deleted,
        )

    def prune_keep_last(
        self, count: int, *, on_deleted: Callable[[DeletedRun], None]
    ) -> PrunedRuns:
        """Delete every finished run except the ``count`` most recently created."""
        if count < 0:
            # A negative count would select more than every finished run.
            raise ValueError("The number of finished runs to keep cannot be negative")
        return self._prune(
            lambda finished: finished[: max(len(finished) - count, 0)], on_deleted
        )

    def clear(self, *, include_schedules: bool) -> ClearedState:
        """Delete every run, result and staged input file; schedules too if asked.

        Processes still alive from an unfinished run are stopped first, as an
        operator start does, since no later operator could find them once the run
        is gone. The rows go in one transaction; then every result bundle and
        input-file folder is removed, including leftovers nothing referred to, and
        every schedule input-file folder no kept schedule uses.
        """
        with self._store.transaction() as connection:
            summaries = RunsTable.list_summaries(connection)
        stopped = tuple(
            StoppedRunProcess(summary.run_id, summary.process.process_group)
            for summary in summaries
            if summary.status in UNFINISHED_STATUSES
            and summary.process is not None
            and stop_run_process(summary.process, grace=DEFAULT_CANCEL_GRACE_SECONDS)
        )
        with self._store.transaction() as connection:
            RunResultTable.delete_all(connection)
            runs = RunsTable.delete_all(connection)
            schedules_deleted = (
                SchedulesTable.delete_all(connection) if include_schedules else 0
            )
            kept = SchedulesTable.read_all(connection)
        try:
            bundles = self._results.remove_unindexed_bundles()
            input_folders, _ = _token_folders(self._input_root)
            for name in input_folders:
                _remove_folder(self._input_root / name)
            used_schedule_folders = {
                input_file.folder for schedule in kept for input_file in schedule.input_files
            }
            schedule_folders, _ = _token_folders(self._schedule_root)
            unused_schedule_folders = [
                name for name in schedule_folders if name not in used_schedule_folders
            ]
            for name in unused_schedule_folders:
                _remove_folder(self._schedule_root / name)
        except (OSError, ValueError) as exc:
            raise StateMaintenanceError(
                f"Every run is already deleted from {self._database}, but removing their "
                f"files failed: {exc}. `ava state info` reports the files left behind, and "
                "`ava state clear` removes them."
            ) from exc
        return ClearedState(
            runs=runs,
            stopped_processes=stopped,
            result_bundles=len(bundles),
            input_file_folders=len(input_folders),
            schedules_deleted=schedules_deleted,
            schedules_kept=len(kept),
            schedule_input_folders=len(unused_schedule_folders),
        )

    def reclaim_space(self, *, always_vacuum: bool) -> SpaceReclaimed:
        """Compact the database after a deletion.

        ``VACUUM`` rebuilds the file without its free pages when ``always_vacuum``
        is set (after a clear) or once :data:`VACUUM_FREE_FRACTION` of it is free.
        Otherwise free pages stay in the file for SQLite to reuse, which spares
        rewriting a large database to delete one run.
        """
        usage = self._store.page_usage()
        vacuumed = always_vacuum or usage.free_fraction >= VACUUM_FREE_FRACTION
        self._store.compact(vacuum=vacuumed)
        return SpaceReclaimed(
            vacuumed=vacuumed,
            free_bytes=usage.free_bytes,
            bytes_before=self._bytes_at_open,
            bytes_after=_database_bytes(self._database),
        )

    def close(self) -> None:
        """Release the result storage and the database; the original error wins."""
        try:
            self._results.close()
        except BaseException as failure:
            _close_after(self._store, failure)
            raise
        self._store.close()

    def _prune(
        self,
        select: Callable[[list[RunSummary]], list[RunSummary]],
        on_deleted: Callable[[DeletedRun], None],
    ) -> PrunedRuns:
        with self._store.transaction() as connection:
            summaries = RunsTable.list_summaries(connection)
        finished = [
            summary for summary in summaries if summary.status not in UNFINISHED_STATUSES
        ]
        return PrunedRuns(
            deleted=self._delete_runs(select(finished), on_deleted),
            unfinished_kept=len(summaries) - len(finished),
        )

    def _delete_runs(
        self, summaries: Sequence[RunSummary], on_deleted: Callable[[DeletedRun], None]
    ) -> tuple[DeletedRun, ...]:
        deleted = []
        for summary in summaries:
            run = self._delete_run(summary)
            on_deleted(run)
            deleted.append(run)
        return tuple(deleted)

    def _delete_run(self, summary: RunSummary) -> DeletedRun:
        run_id = summary.run_id
        with self._store.transaction() as connection:
            result = RunResultTable.delete(connection, run_id)
            details = delete_run_details(connection, run_id)
            keys = RunsTable.input_file_keys(connection, run_id)
            RunsTable.delete(connection, run_id)
            folders = tuple(
                sorted(
                    {
                        _token_folder(key)
                        for key in keys
                        if not RunsTable.input_file_key_in_use(connection, key)
                    }
                )
            )
        try:
            if result is not None:
                self._remove_bundle(result)
            for name in folders:
                _remove_folder(self._input_root / name)
        except (OSError, ValueError) as exc:
            # The result store refuses a bundle that is not a private directory.
            raise StateMaintenanceError(
                f"Run {run_id} is deleted from {self._database}, but removing its files "
                f"failed: {exc}. `ava state info` reports the files it left behind, and "
                "`ava state clear` removes them."
            ) from exc
        return DeletedRun(
            run_id=run_id,
            status=summary.status,
            details=details,
            result_bundle=None if result is None else result.bundle_name,
            input_file_folders=folders,
        )

    def _remove_bundle(self, result: RunResultRow) -> None:
        try:
            self._results.discard(result)
        except FileNotFoundError:
            # Already missing: the index row named a bundle that was gone.
            pass


def _token_folder(object_key: str) -> str:
    token, separator, _ = object_key.partition("/")
    if not separator or _TOKEN.fullmatch(token) is None:
        raise StateRowError(f"Invalid input file key {object_key!r} in run_input_files")
    return token


def _token_folders(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ``<token>`` folders in ``root`` and any other entries, sorted by name."""
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except FileNotFoundError:
        # No operator has created the folder yet.
        return (), ()
    folders = tuple(
        entry.name
        for entry in entries
        if _TOKEN.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
    )
    others = tuple(entry.name for entry in entries if entry.name not in folders)
    return folders, others


def _tree_bytes(root: Path) -> int:
    """The size of every file under ``root``, never following links."""
    total = 0
    for folder, _, names in os.walk(root):
        for name in names:
            total += os.lstat(os.path.join(folder, name)).st_size
    return total


def _remove_folder(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        # Removed already, by an earlier delete that was interrupted.
        pass


def _database_bytes(database: Path) -> int:
    # Sizes come from os.stat only: opening the file would drop the SQLite lock.
    total = database.stat().st_size
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def _close_after(store: OperatorStateStore, failure: BaseException) -> None:
    try:
        store.close()
    except Exception as exc:
        failure.add_note(f"Closing the operator state database also failed: {exc}")
