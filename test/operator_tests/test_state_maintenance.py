"""``ava state``: inspecting and clearing a stopped operator's state database."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ava_cli.app import main
from runtime.operator.input_files import StagedInputFile
from runtime.operator.models import (
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    SequencedLogEntry,
    TriggerKind,
    WorkflowTopology,
)
from runtime.operator.run_processes import RunProcess, RunProcessStopError
from runtime.operator.state import OperatorStateStore, StateStoreLockedError
from runtime.operator.state import maintenance as maintenance_module
from runtime.operator.state.logs import RunLogsTable
from runtime.operator.state.maintenance import (
    DeletedRun,
    StateMaintenance,
    StateMaintenanceError,
)
from runtime.operator.state.results import RunResultTable, StoredWorkflowResult
from runtime.operator.state.runs import RunRequest, RunsTable, StoredRun
from runtime.operator.state.schedule_input_files import ScheduleInputFile
from runtime.operator.state.schedules import SchedulesTable

pytestmark = pytest.mark.skipif(os.name == "nt", reason="result storage needs POSIX")

_DAY = 24 * 60 * 60
_NOW = 1_800_000_000.0


class _StateFolder:
    """A stopped operator's database and folders, filled through the adapters."""

    def __init__(self, root: Path) -> None:
        self.database = root / "state.db"
        self.results = root / "results"
        self.input_files = root / "input-files"
        self.schedule_inputs = root / "schedule-inputs"
        for folder in (self.results, self.input_files, self.schedule_inputs):
            folder.mkdir(mode=0o700, parents=True)
        self._sequence = 0
        OperatorStateStore.open(self.database).close()

    def add_run(
        self,
        run_id: str,
        status: RunStatus = RunStatus.SUCCESS,
        *,
        triggered_at: float = _NOW,
        files: tuple[str, ...] = (),
        result: bool = False,
        logs: int = 0,
        log_bytes: int = 10,
        process: RunProcess | None = None,
    ) -> None:
        """Store a run, its log lines, a result bundle and staged files it used."""
        self._sequence += 1
        run = RunState(
            run_id=run_id,
            flow_name="Flow",
            status=status,
            triggered_at=triggered_at,
            triggered_by=TriggerKind.API,
            workflow_id="flows.py::flow",
            workflow_display_name="Flow",
            topology=WorkflowTopology(
                node_ids=("a",),
                graph=(("a", ()),),
                node_types=(("a", "step"),),
                display_names=(("a", "A"),),
                input_declaration_json='{"fields": []}',
            ),
        )
        run.nodes["a"] = NodeState("a", "A", "step", status=NodeStatus.SUCCESS)
        staged = []
        for name in files:
            token, _, file_name = name.partition("/")
            folder = self.input_files / token
            folder.mkdir(mode=0o700, exist_ok=True)
            (folder / file_name).write_bytes(b"data")
            staged.append(StagedInputFile(name, file_name, "text/plain", "0" * 64, 4))
        store = OperatorStateStore.open(self.database)
        try:
            with store.transaction() as connection:
                RunsTable.create(
                    connection,
                    StoredRun(
                        run=run,
                        created_sequence=self._sequence,
                        revision=1,
                        activity_sequence=0,
                        node_revisions={"a": 1},
                    ),
                    RunRequest(input_files=tuple(staged)),
                    process,
                )
                for sequence in range(1, logs + 1):
                    RunLogsTable.insert(
                        connection,
                        run_id,
                        SequencedLogEntry(
                            sequence,
                            LogEntry(datetime.now(UTC), LogLevel.INFO, "a", "x" * log_bytes),
                            log_bytes,
                        ),
                    )
                if result:
                    RunResultTable.insert(connection, run_id, self.bundle())
        finally:
            store.close()

    def bundle(self) -> StoredWorkflowResult:
        """Write an accepted bundle directory and return its index entry."""
        name = f"bundle_{uuid.uuid4().hex}"
        (self.results / name).mkdir(mode=0o700)
        (self.results / name / "manifest.json").write_bytes(b"{}")
        return StoredWorkflowResult(
            bundle_name=name, manifest_sha256="0" * 64, byte_size=2, published_at=_NOW
        )

    def add_schedule(self, workflow_id: str, folder: str) -> None:
        (self.schedule_inputs / folder).mkdir(mode=0o700)
        (self.schedule_inputs / folder / "input.txt").write_bytes(b"schedule")
        store = OperatorStateStore.open(self.database)
        try:
            with store.transaction() as connection:
                SchedulesTable.write_input(
                    connection,
                    workflow_id,
                    enabled=True,
                    enabled_minute=None,
                    run_input={},
                    input_files=(
                        ScheduleInputFile(
                            folder=folder,
                            name="input.txt",
                            workspace_reference=f"local-ws:v1:{folder}",
                        ),
                    ),
                )
        finally:
            store.close()

    def run_ids(self) -> list[str]:
        store = OperatorStateStore.open(self.database)
        try:
            with store.transaction() as connection:
                return [summary.run_id for summary in RunsTable.list_summaries(connection)]
        finally:
            store.close()

    def log_count(self, run_id: str) -> int:
        store = OperatorStateStore.open(self.database)
        try:
            with store.transaction() as connection:
                return len(RunLogsTable.list_run(connection, run_id))
        finally:
            store.close()

    def bundles(self) -> set[str]:
        return {path.name for path in self.results.glob("bundle_*")}


def _token(character: str) -> str:
    return character * 32


@pytest.fixture
def folder(tmp_path) -> _StateFolder:
    return _StateFolder(tmp_path / "operator")


# ── Info ─────────────────────────────────────────────────


def test_info_counts_runs_files_and_reports_leftovers(folder):
    folder.add_run("done", files=(f"{_token('a')}/a.txt",), result=True)
    folder.add_run("broken", RunStatus.FAILED)
    folder.add_run("interrupted", RunStatus.RUNNING)
    folder.add_schedule("flows.py::flow", _token("c"))
    stray_bundle = folder.bundle().bundle_name
    (folder.input_files / _token("b")).mkdir(mode=0o700)
    (folder.schedule_inputs / _token("d")).mkdir(mode=0o700)
    (folder.input_files / "notes.txt").write_bytes(b"mine")

    with StateMaintenance.open(folder.database) as state:
        info = state.info()

    assert info.database == folder.database.resolve()
    assert info.database_bytes >= folder.database.stat().st_size
    assert dict(info.runs_by_status) == {
        RunStatus.SUCCESS: 1,
        RunStatus.FAILED: 1,
        RunStatus.RUNNING: 1,
    }
    assert info.unfinished_runs == 1
    assert info.schedules == 1
    assert info.results.entries == 1
    assert info.input_files.entries == 2
    assert info.input_files.size_bytes == len(b"data") + len(b"mine")
    assert info.schedule_inputs.entries == 2
    assert info.result_index.unindexed_bundles == (stray_bundle,)
    assert info.unreferenced_input_files == (folder.input_files.resolve() / _token("b"),)
    assert info.unreferenced_schedule_inputs == (
        folder.schedule_inputs.resolve() / _token("d"),
    )
    assert info.unexpected_entries == (folder.input_files.resolve() / "notes.txt",)


def test_info_reports_a_stored_result_whose_bundle_is_gone(folder):
    folder.add_run("done", result=True)
    for bundle in folder.results.glob("bundle_*"):
        (bundle / "manifest.json").unlink()
        bundle.rmdir()

    with StateMaintenance.open(folder.database) as state:
        missing = state.info().result_index.missing_bundles

    assert [row.run_id for row in missing] == ["done"]


# ── Delete ───────────────────────────────────────────────


def test_delete_removes_the_run_its_details_result_and_unshared_files(folder):
    shared = f"{_token('a')}/shared.txt"
    own = f"{_token('b')}/own.txt"
    folder.add_run("first", files=(shared, own), result=True, logs=3)
    folder.add_run("second", files=(shared,), result=True, logs=1)
    bundles = folder.bundles()

    with StateMaintenance.open(folder.database) as state:
        (deleted,) = state.delete(["first"], on_deleted=_ignore)

    assert deleted.run_id == "first"
    assert deleted.details.logs == 3
    assert deleted.result_bundle is not None
    assert deleted.input_file_folders == (_token("b"),)
    assert folder.run_ids() == ["second"]
    assert folder.log_count("first") == 0
    assert folder.log_count("second") == 1
    assert folder.bundles() == bundles - {deleted.result_bundle}
    assert (folder.input_files / _token("a") / "shared.txt").exists()
    assert not (folder.input_files / _token("b")).exists()

    with StateMaintenance.open(folder.database) as state:
        (deleted,) = state.delete(["second"], on_deleted=_ignore)

    assert deleted.input_file_folders == (_token("a"),)
    assert not (folder.input_files / _token("a")).exists()
    assert folder.bundles() == set()


@pytest.mark.parametrize(
    ("run_ids", "message"),
    [
        pytest.param(["done", "missing"], "missing", id="unknown"),
        pytest.param(["done", "interrupted"], "interrupted", id="unfinished"),
    ],
)
def test_delete_refuses_unknown_or_unfinished_runs_before_deleting_anything(
    folder, run_ids, message
):
    folder.add_run("done", result=True)
    folder.add_run("interrupted", RunStatus.PENDING)

    with StateMaintenance.open(folder.database) as state:
        with pytest.raises(StateMaintenanceError, match=message):
            state.delete(run_ids, on_deleted=_ignore)

    assert folder.run_ids() == ["done", "interrupted"]
    assert len(folder.bundles()) == 1


def test_a_failed_delete_transaction_leaves_the_whole_run(folder, monkeypatch):
    folder.add_run("done", files=(f"{_token('a')}/a.txt",), result=True, logs=2)

    def fail(connection, run_id):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(maintenance_module.RunsTable, "delete", fail)
    with StateMaintenance.open(folder.database) as state:
        with pytest.raises(RuntimeError, match="interrupted"):
            state.delete(["done"], on_deleted=_ignore)
        info = state.info()

    assert folder.run_ids() == ["done"]
    assert folder.log_count("done") == 2
    assert info.results.entries == 1
    assert info.result_index.consistent
    assert (folder.input_files / _token("a") / "a.txt").exists()


def test_files_left_by_a_delete_interrupted_after_its_commit_are_reported_then_cleared(
    folder, monkeypatch
):
    folder.add_run("done", files=(f"{_token('a')}/a.txt",), result=True)
    bundle = next(iter(folder.bundles()))

    def fail(self, result):
        raise OSError("disk went away")

    with monkeypatch.context() as patch:
        patch.setattr(maintenance_module.ResultStore, "discard", fail)
        with StateMaintenance.open(folder.database) as state:
            with pytest.raises(StateMaintenanceError, match="already deleted|is deleted"):
                state.delete(["done"], on_deleted=_ignore)

    assert folder.run_ids() == []
    with StateMaintenance.open(folder.database) as state:
        info = state.info()
        assert info.result_index.unindexed_bundles == (bundle,)
        assert info.unreferenced_input_files == (folder.input_files.resolve() / _token("a"),)
        cleared = state.clear(include_schedules=False)

    assert (cleared.result_bundles, cleared.input_file_folders) == (1, 1)
    assert folder.bundles() == set()
    assert list(folder.input_files.iterdir()) == []


def test_a_failure_part_way_through_reports_the_runs_already_deleted(folder, monkeypatch):
    folder.add_run("first", result=True)
    folder.add_run("second", result=True)
    folder.add_run("third", result=True)
    removals = []

    def discard(self, result):
        removals.append(result.run_id)
        if len(removals) == 2:
            raise OSError("disk went away")

    monkeypatch.setattr(maintenance_module.ResultStore, "discard", discard)
    reported = []
    with StateMaintenance.open(folder.database) as state:
        with pytest.raises(StateMaintenanceError):
            state.delete(
                ["first", "second", "third"],
                on_deleted=lambda run: reported.append(run.run_id),
            )

    assert reported == ["first"]
    assert folder.run_ids() == ["third"]


# ── Prune ────────────────────────────────────────────────


def test_prune_keep_last_keeps_the_newest_finished_runs_and_every_unfinished_one(folder):
    folder.add_run("old", RunStatus.SUCCESS)
    folder.add_run("stuck", RunStatus.RUNNING)
    folder.add_run("middle", RunStatus.CANCELLED)
    folder.add_run("waiting", RunStatus.REQUESTING)
    folder.add_run("new", RunStatus.FAILED)

    with StateMaintenance.open(folder.database) as state:
        pruned = state.prune_keep_last(1, on_deleted=_ignore)

    assert [run.run_id for run in pruned.deleted] == ["old", "middle"]
    assert pruned.unfinished_kept == 2
    assert folder.run_ids() == ["stuck", "waiting", "new"]


def test_prune_keep_last_zero_deletes_every_finished_run_but_no_unfinished_one(folder):
    folder.add_run("done")
    folder.add_run("stuck", RunStatus.PENDING)

    with StateMaintenance.open(folder.database) as state:
        pruned = state.prune_keep_last(0, on_deleted=_ignore)

    assert [run.run_id for run in pruned.deleted] == ["done"]
    assert folder.run_ids() == ["stuck"]


def test_prune_before_deletes_finished_runs_triggered_before_the_cutoff(folder):
    folder.add_run("old", triggered_at=_NOW - 3 * _DAY)
    folder.add_run("old-unfinished", RunStatus.RUNNING, triggered_at=_NOW - 3 * _DAY)
    folder.add_run("recent", triggered_at=_NOW)

    with StateMaintenance.open(folder.database) as state:
        pruned = state.prune_before(
            datetime.fromtimestamp(_NOW - _DAY, UTC), on_deleted=_ignore
        )

    assert [run.run_id for run in pruned.deleted] == ["old"]
    assert folder.run_ids() == ["old-unfinished", "recent"]


def test_prune_refuses_a_negative_count_or_a_date_without_time_zone(folder):
    folder.add_run("done")

    with StateMaintenance.open(folder.database) as state:
        with pytest.raises(ValueError):
            state.prune_keep_last(-1, on_deleted=_ignore)
        with pytest.raises(ValueError):
            state.prune_before(datetime(2026, 11, 1), on_deleted=_ignore)

    assert folder.run_ids() == ["done"]


# ── Clear ────────────────────────────────────────────────


def test_clear_deletes_every_run_and_file_but_keeps_schedules_and_their_files(folder):
    folder.add_run("done", files=(f"{_token('a')}/a.txt",), result=True, logs=2)
    folder.add_run("stuck", RunStatus.RUNNING)
    folder.add_schedule("flows.py::flow", _token("c"))
    folder.bundle()
    (folder.input_files / _token("b")).mkdir(mode=0o700)
    (folder.schedule_inputs / _token("d")).mkdir(mode=0o700)

    with StateMaintenance.open(folder.database) as state:
        cleared = state.clear(include_schedules=False)
        info = state.info()

    assert cleared.runs == 2
    assert cleared.result_bundles == 2
    assert cleared.input_file_folders == 2
    assert (cleared.schedules_deleted, cleared.schedules_kept) == (0, 1)
    assert cleared.schedule_input_folders == 1
    assert info.runs == 0
    assert info.schedules == 1
    assert folder.bundles() == set()
    assert list(folder.input_files.iterdir()) == []
    assert [path.name for path in folder.schedule_inputs.iterdir()] == [_token("c")]


def test_clear_with_schedules_forgets_them_and_their_files(folder):
    folder.add_schedule("flows.py::flow", _token("c"))

    with StateMaintenance.open(folder.database) as state:
        cleared = state.clear(include_schedules=True)
        assert state.info().schedules == 0

    assert (cleared.schedules_deleted, cleared.schedules_kept) == (1, 0)
    assert cleared.schedule_input_folders == 1
    assert list(folder.schedule_inputs.iterdir()) == []


def test_clear_stops_processes_left_by_unfinished_runs_first(folder, monkeypatch):
    process = RunProcess(process_group=4321, started="then", booted="boot")
    folder.add_run("stuck", RunStatus.RUNNING, process=process)
    folder.add_run("done", process=RunProcess(process_group=99, started="a", booted="b"))
    stopped = []

    def stop(recorded, *, grace):
        stopped.append(recorded)
        return True

    monkeypatch.setattr(maintenance_module, "stop_run_process", stop)
    with StateMaintenance.open(folder.database) as state:
        cleared = state.clear(include_schedules=False)

    assert stopped == [process]
    assert [(item.run_id, item.process_group) for item in cleared.stopped_processes] == [
        ("stuck", 4321)
    ]


def test_clear_deletes_nothing_when_a_left_over_process_cannot_be_stopped(folder, monkeypatch):
    folder.add_run(
        "stuck",
        RunStatus.RUNNING,
        process=RunProcess(process_group=4321, started="then", booted="boot"),
    )

    def stop(recorded, *, grace):
        raise RunProcessStopError("still alive")

    monkeypatch.setattr(maintenance_module, "stop_run_process", stop)
    with StateMaintenance.open(folder.database) as state:
        with pytest.raises(RunProcessStopError):
            state.clear(include_schedules=False)

    assert folder.run_ids() == ["stuck"]


# ── Space ────────────────────────────────────────────────


def test_a_large_deletion_vacuums_and_a_small_one_leaves_free_pages(folder):
    folder.add_run("big", logs=200, log_bytes=4000)
    folder.add_run("small", logs=1)
    folder.add_run("rest", logs=1)

    with StateMaintenance.open(folder.database) as state:
        state.delete(["small"], on_deleted=_ignore)
        small = state.reclaim_space(always_vacuum=False)
    with StateMaintenance.open(folder.database) as state:
        state.delete(["big"], on_deleted=_ignore)
        large = state.reclaim_space(always_vacuum=False)

    assert not small.vacuumed
    assert large.vacuumed
    assert large.bytes_after < large.bytes_before / 2
    assert folder.database.stat().st_size == large.bytes_after


# ── Locking and command line ─────────────────────────────


def test_every_command_refuses_while_an_operator_holds_the_database(folder, capsys):
    folder.add_run("done")
    holder = OperatorStateStore.open(folder.database)
    try:
        with pytest.raises(StateStoreLockedError):
            StateMaintenance.open(folder.database)
        for command in (
            ["info"],
            ["delete", "done"],
            ["prune", "--keep-last", "0"],
            ["clear", "--yes"],
        ):
            assert main(["state", *command, "--state-db", str(folder.database)]) == 1
            assert str(folder.database) in capsys.readouterr().err
    finally:
        holder.close()
    assert folder.run_ids() == ["done"]


@pytest.mark.parametrize("state_db", [":memory:", "missing/state.db"])
def test_commands_refuse_a_database_that_does_not_exist(tmp_path, state_db, capsys):
    path = state_db if state_db == ":memory:" else str(tmp_path / state_db)

    assert main(["state", "info", "--state-db", path]) == 1

    assert path in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    ("answer", "cleared"),
    [pytest.param("y\n", True, id="yes"), pytest.param("n\n", False, id="no")],
)
def test_clear_asks_before_deleting(folder, monkeypatch, answer, cleared):
    folder.add_run("done")
    monkeypatch.setattr("sys.stdin", _Input(answer))

    code = main(["state", "clear", "--state-db", str(folder.database)])

    assert code == (0 if cleared else 1)
    assert folder.run_ids() == ([] if cleared else ["done"])


def test_clear_without_an_answer_deletes_nothing(folder, monkeypatch):
    folder.add_run("done")
    monkeypatch.setattr("sys.stdin", _Input(""))

    assert main(["state", "clear", "--state-db", str(folder.database)]) == 1
    assert folder.run_ids() == ["done"]


def test_cli_info_delete_and_prune_change_the_database(folder, capsys):
    folder.add_run("one", result=True, logs=2)
    folder.add_run("two")
    folder.add_run("three")
    database = str(folder.database)

    assert main(["state", "info", "--state-db", database]) == 0
    assert main(["state", "delete", "one", "--state-db", database]) == 0
    assert folder.run_ids() == ["two", "three"]
    assert main(["state", "prune", "--keep-last", "1", "--state-db", database]) == 0
    assert folder.run_ids() == ["three"]
    # Each deleted run is reported by its ID.
    output = capsys.readouterr().out
    assert "one" in output and "two" in output


@pytest.mark.parametrize("argument", [["--keep-last", "-1"], ["--before", "yesterday"]])
def test_prune_rejects_a_bad_selection(folder, argument):
    with pytest.raises(SystemExit) as exited:
        main(["state", "prune", *argument, "--state-db", str(folder.database)])
    assert exited.value.code == 2


def test_prune_before_reads_a_date_without_time_zone_as_local_time(folder):
    folder.add_run("old", triggered_at=datetime(2026, 10, 31, 23, 0).timestamp())
    folder.add_run("new", triggered_at=datetime(2026, 11, 1, 1, 0).timestamp())

    database = str(folder.database)
    assert main(["state", "prune", "--before", "2026-11-01", "--state-db", database]) == 0

    assert folder.run_ids() == ["new"]


def _ignore(run: DeletedRun) -> None:
    pass


class _Input:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""
