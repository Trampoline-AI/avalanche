"""Real spawned workers: live reload, isolation, shutdown, and process death."""

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.operator import Operator
from runtime.operator import operator as operator_module
from runtime.operator.models import (
    NodeStatus,
    RunStatus,
    RunStatusChanged,
    TerminalSealAppended,
)


def _wait_terminal(operator: Operator, run_id: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.03)
    raise AssertionError(f"run {run_id} did not finish")


def _wait_inactive(operator: Operator, run_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run_id not in operator._active_runs:
            return
        time.sleep(0.03)
    raise AssertionError(f"coordinator for {run_id} was not reaped")


def _write_standalone(root: Path, *, deferred: bool = False, body: str | None = None) -> Path:
    (root / "helper.py").write_text("VALUE = 1\n")
    import_line = "" if deferred else "from helper import VALUE\n"
    value_line = "from helper import VALUE\n    " if deferred else ""
    node_body = body or f"time.sleep(0.35)\n    {value_line}log.info(f'value={{VALUE}}')"
    workflow = root / "flow.py"
    workflow.write_text(
        "import time\n"
        "import avalanche as ava\n"
        f"{import_line}"
        "@ava.source\n"
        "def read(log=ava.Logger()):\n"
        f"    {node_body}\n"
        "@ava.workflow\n"
        "def flow():\n"
        "    read()\n"
    )
    return workflow


@pytest.mark.parametrize("deferred", [False, True])
def test_current_and_later_runs_use_live_source(tmp_path, deferred):
    workflow = _write_standalone(tmp_path, deferred=deferred)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run_a = operator.start_run("flow")
        (tmp_path / "helper.py").write_text("VALUE = 2\n")
        operator._refresh_workflows()
        run_b = operator.start_run("flow")

        state_a = _wait_terminal(operator, run_a)
        state_b = _wait_terminal(operator, run_b)
        assert state_a.status == RunStatus.SUCCESS
        assert state_b.status == RunStatus.SUCCESS
        assert any("value=2" in entry.message for entry in state_a.logs)
        assert any("value=2" in entry.message for entry in state_b.logs)
    finally:
        operator.close()


def test_concurrent_runs_have_isolated_module_globals(tmp_path):
    workflow = _write_standalone(
        tmp_path,
        body=(
            "global COUNTER\n    time.sleep(0.25)\n    COUNTER += 1\n"
            "    log.info(f'counter={COUNTER}')"
        ),
    )
    text = workflow.read_text().replace(
        "import avalanche as ava\n", "import avalanche as ava\nCOUNTER = 0\n"
    )
    workflow.write_text(text)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run_ids = [operator.start_run("flow"), operator.start_run("flow")]
        runs = [_wait_terminal(operator, run_id) for run_id in run_ids]
        assert all(run.status == RunStatus.SUCCESS for run in runs)
        assert all(any("counter=1" in entry.message for entry in run.logs) for run in runs)
    finally:
        operator.close()


def test_prepare_failure_publishes_failed_run(tmp_path):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        workflow.write_text("this is invalid Python !!!\n")
        run = _wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.FAILED
        _wait_inactive(operator, run.run_id)
    finally:
        operator.close()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "crash"])
def test_coordinator_cleanup_on_terminal_paths(tmp_path, outcome):
    bodies = {
        "success": "log.info('finished')",
        "failure": "raise RuntimeError('boom')",
        "cancel": "time.sleep(1.0)\n    log.info('finished')",
        "crash": "os._exit(17)",
    }
    workflow = _write_standalone(tmp_path, body=bodies[outcome])
    if outcome == "crash":
        workflow.write_text("import os\n" + workflow.read_text())
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run_id = operator.start_run("flow")
        if outcome == "cancel":
            operator.cancel_run(run_id)
        run = _wait_terminal(operator, run_id)
        expected = {
            "success": RunStatus.SUCCESS,
            "failure": RunStatus.FAILED,
            "cancel": RunStatus.CANCELLED,
            "crash": RunStatus.FAILED,
        }[outcome]
        assert run.status == expected
        _wait_inactive(operator, run_id)
    finally:
        operator.close()


def test_cancel_request_is_non_terminal_until_coordinator_stops(tmp_path):
    workflow = _write_standalone(tmp_path, body="time.sleep(10)")
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        cancel_grace=0.15,
    )
    updates = operator.subscribe_operator_updates()
    try:
        run_id = operator.start_run("flow")
        deadline = time.monotonic() + 8
        while operator.get_run(run_id).status != RunStatus.RUNNING:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        operator.cancel_run(run_id)
        assert operator.get_run(run_id).status == RunStatus.RUNNING

        run = _wait_terminal(operator, run_id)
        assert run.status == RunStatus.CANCELLED
        terminal_log_count = len(run.logs)
        time.sleep(0.2)
        assert len(operator.get_run(run_id).logs) == terminal_log_count
        _wait_inactive(operator, run_id)

        queued = []
        while not updates.empty():
            queued.append(updates.get_nowait().update.change)
        terminal_indexes = [
            index
            for index, change in enumerate(queued)
            if isinstance(change, RunStatusChanged) and change.status == RunStatus.CANCELLED
        ]
        assert len(terminal_indexes) == 1
        assert all(node.status == NodeStatus.SKIPPED for node in run.nodes.values())
        assert isinstance(queued[-1], TerminalSealAppended)
        assert queued[-1].seal.terminal_status is RunStatus.CANCELLED
    finally:
        operator.close()


def test_close_cancels_run_after_start_returns_id(tmp_path):
    workflow = _write_standalone(tmp_path, body="time.sleep(10)")
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        cancel_grace=0.1,
    )
    run_id = operator.start_run("flow")
    handle = operator._active_runs[run_id]

    started = time.monotonic()
    operator.close()

    assert time.monotonic() - started < 3.0
    assert not handle.process.is_alive()
    assert operator.get_run(run_id).status == RunStatus.CANCELLED
    assert operator._active_runs == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_preparation_timeout_kills_sigterm_ignoring_coordinator(tmp_path):
    workflow = _write_standalone(tmp_path)
    pid_file = tmp_path / "preparing.pid"
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        prepare_timeout=5.0,
    )
    try:
        workflow.write_text(
            "import os, signal, time\n"
            "from pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "import avalanche as ava\n"
            "time.sleep(30)\n"
            "@ava.workflow\n"
            "def flow():\n"
            "    return None\n"
        )
        started = time.monotonic()
        run = _wait_terminal(operator, operator.start_run("flow"), timeout=8.0)
        assert run.status == RunStatus.FAILED
        assert time.monotonic() - started < 8.0
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert operator._active_runs == {}
    finally:
        operator.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_running_cancellation_kills_sigterm_ignoring_coordinator(tmp_path):
    pid_file = tmp_path / "running.pid"
    workflow = tmp_path / "flow.py"
    workflow.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "import avalanche as ava\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "@ava.source\n"
        "def hang():\n"
        "    while True:\n"
        "        time.sleep(0.1)\n"
        "@ava.workflow\n"
        "def flow():\n"
        "    hang()\n"
    )
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        cancel_grace=0.1,
    )
    try:
        run_id = operator.start_run("flow")
        deadline = time.monotonic() + 8
        while operator.get_run(run_id).status != RunStatus.RUNNING:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        operator.cancel_run(run_id)
        run = _wait_terminal(operator, run_id)
        assert run.status == RunStatus.CANCELLED
        _wait_inactive(operator, run_id)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        operator.close()


def test_watcher_refreshes_cron_imported_from_live_package_root(tmp_path):
    project = tmp_path / "project"
    package = project / "workflows"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    schedule = project / "schedule.py"
    schedule.write_text('CRON = "3 * * * *"\n')
    workflow = package / "flow.py"
    workflow.write_text(
        "import avalanche as ava\n"
        "from schedule import CRON\n"
        "@ava.workflow(cron=CRON)\n"
        "def flow():\n"
        "    return None\n"
    )
    operator = Operator([str(workflow)], watch=True, schedule=False)
    try:
        assert operator.list_workflows()[0].cron == "3 * * * *"
        schedule.write_text('CRON = "4,5 * * * *"\n')
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if operator.list_workflows()[0].cron == "4,5 * * * *":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("import-root change did not refresh workflow cron")
    finally:
        operator.close()


def test_watcher_refreshes_resource_derived_cron(tmp_path):
    config = tmp_path / "schedule.json"
    config.write_text('{"cron": "1 * * * *"}')
    workflow = tmp_path / "flow.py"
    workflow.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "import avalanche as ava\n"
        "CRON = json.loads(Path(__file__).with_name('schedule.json').read_text())['cron']\n"
        "@ava.workflow(cron=CRON)\n"
        "def flow():\n"
        "    return None\n"
    )
    operator = Operator([str(tmp_path)], watch=True, schedule=False)
    try:
        assert operator.list_workflows()[0].cron == "1 * * * *"
        config.write_text('{"cron": "2 * * * *"}')
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if operator.list_workflows()[0].cron == "2 * * * *":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("resource change did not refresh workflow cron")
    finally:
        operator.close()


def test_close_rejects_run_when_shutdown_precedes_preparation(tmp_path, monkeypatch):
    workflow = _write_standalone(tmp_path)
    workflow.write_text(
        workflow.read_text().replace(
            "import time\n",
            "import multiprocessing\nimport time\n"
            "if multiprocessing.current_process().name.startswith('avalanche-run-'):\n"
            "    time.sleep(5.0)\n",
            1,
        )
    )
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        cancel_grace=0.1,
    )
    publication_delivered = threading.Event()
    release_start = threading.Event()
    original_wait_for_notifications = operator._wait_for_notifications

    def pause_after_run_is_published(notifications):
        original_wait_for_notifications(notifications)
        publication_delivered.set()
        if not release_start.wait(timeout=5):
            raise TimeoutError("Timed out waiting to release run startup")

    monkeypatch.setattr(operator, "_wait_for_notifications", pause_after_run_is_published)
    errors: list[BaseException] = []
    run_ids: list[str] = []

    def start():
        try:
            run_ids.append(operator.start_run("flow"))
        except BaseException as exc:
            errors.append(exc)

    starter = threading.Thread(target=start)
    try:
        starter.start()
        assert publication_delivered.wait(timeout=5)

        operator.close()
        release_start.set()
        starter.join(timeout=3)

        assert not starter.is_alive()
        assert run_ids == []
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert operator._runs == {}
        assert operator._active_runs == {}
    finally:
        release_start.set()
        operator.close()
        if starter.ident is not None:
            starter.join(timeout=3)


@pytest.mark.parametrize(
    ("process_table", "expected"),
    [
        ("4242 Z\n4242 Z+\n", False),
        ("4242 ?E\n", False),
        ("4242 Z\n4242 S+\n", True),
    ],
)
def test_process_group_quiescence_distinguishes_zombies_from_live_descendants(
    monkeypatch,
    process_table,
    expected,
):
    monkeypatch.setattr(
        operator_module,
        "_coordinator_group_exists",
        lambda _process_group: True,
    )
    monkeypatch.setattr(
        operator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=process_table),
    )

    assert operator_module._coordinator_group_has_live_members(4242) is expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_process_group_permission_error_is_not_quiescence(monkeypatch):
    def deny_process_group_access(_process_group, _signal_number):
        raise PermissionError

    monkeypatch.setattr(
        operator_module.os,
        "killpg",
        deny_process_group_access,
    )
    monkeypatch.setattr(
        operator_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("process table must not be consulted"),
    )

    assert operator_module._coordinator_group_exists(4242) is None
    assert operator_module._coordinator_group_has_live_members(4242)
