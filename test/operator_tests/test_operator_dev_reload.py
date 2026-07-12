import os
import sys
import threading
import time
from pathlib import Path

import pytest

from avalanche.operator import Operator
from avalanche.operator.models import RunStatus
from runtime.operator.source import is_source_path_included


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
        expected_a = 2 if deferred else 1
        assert any(f"value={expected_a}" in entry.message for entry in state_a.logs)
        assert any("value=2" in entry.message for entry in state_b.logs)
    finally:
        operator.close()


@pytest.mark.parametrize("import_style", ["relative", "absolute", "importlib"])
def test_package_import_styles_use_normal_live_import_root(tmp_path, import_style):
    package = tmp_path / "project" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("VALUE = 7\n")
    imports = {
        "relative": "from .helper import VALUE\n",
        "absolute": "from pkg.helper import VALUE\n",
        "importlib": "import importlib\nVALUE = importlib.import_module('pkg.helper').VALUE\n",
    }[import_style]
    workflow = package / "flow.py"
    workflow.write_text(
        "import avalanche as ava\n"
        f"{imports}"
        "@ava.source\n"
        "def read(log=ava.Logger()):\n"
        "    log.info(f'value={VALUE}')\n"
        "@ava.workflow\n"
        "def flow():\n"
        "    read()\n"
    )
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run = _wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert any("value=7" in entry.message for entry in run.logs)
    finally:
        operator.close()


def test_nested_standalone_workflow_imports_sibling(tmp_path):
    nested = tmp_path / "configured" / "nested"
    nested.mkdir(parents=True)
    _write_standalone(nested)
    operator = Operator([str(tmp_path / "configured")], watch=False, schedule=False)
    try:
        run = _wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
        assert any("value=1" in entry.message for entry in run.logs)
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


def test_prepare_failure_does_not_publish_run(tmp_path):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        workflow.write_text("this is invalid Python !!!\n")
        with pytest.raises(RuntimeError, match="preparation failed"):
            operator.start_run("flow")
        assert operator._runs == {}
    finally:
        operator.close()


def test_builder_prepare_failure_does_not_publish_run(tmp_path):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        workflow.write_text(
            workflow.read_text().replace(
                "def flow():\n    read()",
                "def flow():\n    raise RuntimeError('build')",
            )
        )
        with pytest.raises(RuntimeError, match="RuntimeError: build"):
            operator.start_run("flow")
        assert operator._runs == {}
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


def test_operator_never_uses_registry_get_builder(tmp_path, monkeypatch):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    monkeypatch.setattr(
        operator._registry,
        "get_builder",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    try:
        run = _wait_terminal(operator, operator.start_run("flow"))
        assert run.status == RunStatus.SUCCESS
    finally:
        operator.close()


def test_deferred_import_uses_live_package_import_root(tmp_path):
    project = tmp_path / "project"
    package = project / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (project / "outside_helper.py").write_text("VALUE = 1\n")
    workflow = package / "flow.py"
    workflow.write_text(
        "import time\n"
        "import avalanche as ava\n"
        "@ava.source\n"
        "def read(log=ava.Logger()):\n"
        "    time.sleep(0.2)\n"
        "    from outside_helper import VALUE\n"
        "    log.info(f'value={VALUE}')\n"
        "@ava.workflow\n"
        "def flow():\n"
        "    read()\n"
    )
    sys.path.insert(0, str(project))
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run_id = operator.start_run("flow")
        (project / "outside_helper.py").write_text("VALUE = 2\n")
        run = _wait_terminal(operator, run_id)
        assert run.status == RunStatus.SUCCESS
        assert any("value=2" in item.message for item in run.logs)
    finally:
        operator.close()
        sys.path.remove(str(project))


def test_cancel_request_is_non_terminal_until_coordinator_stops(tmp_path):
    workflow = _write_standalone(tmp_path, body="time.sleep(10)")
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        cancel_grace=0.15,
    )
    updates = operator.subscribe()
    try:
        run_id = operator.start_run("flow")
        deadline = time.monotonic() + 2
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
            queued.append(updates.get_nowait()[1])
        terminal_indexes = [
            index for index, state in enumerate(queued) if state.status == RunStatus.CANCELLED
        ]
        assert terminal_indexes == [len(queued) - 1]
    finally:
        operator.close()


def test_slow_subscriber_receives_distinct_coherent_run_snapshots(tmp_path):
    workflow = _write_standalone(
        tmp_path,
        body="log.info('first')\n    log.info('second')",
    )
    operator = Operator([str(workflow)], watch=False, schedule=False)
    subscription = operator.subscribe()
    try:
        run_id = operator.start_run("flow")
        terminal = _wait_terminal(operator, run_id)
        assert terminal.status == RunStatus.SUCCESS

        snapshots = []
        while not subscription.empty():
            snapshots.append(subscription.get_nowait()[1])
        assert len({id(state) for state in snapshots}) == len(snapshots)
        assert snapshots[0].status == RunStatus.PENDING
        assert RunStatus.RUNNING in {state.status for state in snapshots}
        assert snapshots[-1].status == RunStatus.SUCCESS
        log_counts = [len(state.logs) for state in snapshots]
        assert log_counts == sorted(log_counts)
        assert 1 in log_counts and 2 in log_counts
    finally:
        operator.close()


def test_close_boundedly_reaps_active_coordinator(tmp_path):
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
        prepare_timeout=1.5,
    )
    try:
        workflow.write_text(
            "import os, signal, time\n"
            "from pathlib import Path\n"
            "import avalanche as ava\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
            "@ava.workflow\n"
            "def flow():\n"
            "    return None\n"
        )
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="preparation exceeded"):
            operator.start_run("flow")
        assert time.monotonic() - started < 4.5
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
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
        operator.cancel_run(run_id)
        run = _wait_terminal(operator, run_id)
        assert run.status == RunStatus.CANCELLED
        _wait_inactive(operator, run_id)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
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


def test_watch_policy_includes_source_resources_and_excludes_generated_secrets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    assert is_source_path_included(source / "flow.py", (source,))
    assert is_source_path_included(source / "schedule.json", (source,))
    assert not is_source_path_included(source / "__pycache__" / "flow.pyc", (source,))
    assert not is_source_path_included(source / ".env.local", (source,))
    assert not is_source_path_included(source / "credentials.json", (source,))
    assert not is_source_path_included(tmp_path / "outside.py", (source,))


def test_close_owns_and_terminates_run_during_preparation(tmp_path):
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
    errors: list[BaseException] = []

    def start():
        try:
            operator.start_run("flow")
        except BaseException as exc:
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not operator._active_runs:
        time.sleep(0.01)
    assert operator._active_runs

    operator.close()
    starter.join(timeout=3.0)

    assert not starter.is_alive()
    assert errors
    assert operator._active_runs == {}


def test_run_queries_return_detached_snapshots(tmp_path):
    workflow = _write_standalone(tmp_path, body="log.info('finished')")
    operator = Operator([str(workflow)], watch=False, schedule=False)
    try:
        run_id = operator.start_run("flow")
        state = _wait_terminal(operator, run_id)
        state.status = RunStatus.FAILED
        state.logs.clear()

        fresh = operator.get_run(run_id)
        assert fresh is not None
        assert fresh.status == RunStatus.SUCCESS
        assert fresh.logs

        listed = operator.list_runs(fresh.workflow_id)
        listed[0].status = RunStatus.FAILED
        after_list = operator.get_run(run_id)
        assert after_list is not None
        assert after_list.status == RunStatus.SUCCESS
    finally:
        operator.close()


def test_setup_failure_does_not_publish_or_activate_run(tmp_path, monkeypatch):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)

    def fail_queue():
        raise RuntimeError("queue setup failed")

    monkeypatch.setattr(operator._mp, "Queue", fail_queue)
    with pytest.raises(RuntimeError, match="queue setup failed"):
        operator.start_run("flow")

    assert operator._runs == {}
    assert operator._active_runs == {}
    operator.close()
