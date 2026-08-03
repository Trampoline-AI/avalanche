import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from avalanche.operator import Operator
from avalanche.operator.models import (
    LogAppended,
    LogDetailAppended,
    LogLevel,
    NodeState,
    NodeStatus,
    RunCreated,
    RunState,
    RunStatus,
    RunStatusChanged,
)
from runtime.operator import operator as operator_module
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


class _CloseableQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


class _InertProcess:
    pid = None
    exitcode = None

    def is_alive(self):
        return True


def _protocol_test_handle(result_bundle, *, cancelled=False):
    event_queue = _CloseableQueue()
    cancel_event = threading.Event()
    if cancelled:
        cancel_event.set()
    publication_event = threading.Event()
    publication_event.set()
    return SimpleNamespace(
        process=_InertProcess(),
        event_queue=event_queue,
        cancel_event=cancel_event,
        start_event=threading.Event(),
        assignment_event=threading.Event(),
        windows_job=None,
        result_bundle=result_bundle,
        publication_event=publication_event,
        drain_thread=None,
    )


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


class _ExitedProcess:
    pid = 4242
    exitcode = 0

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _install_fake_teardown_clock(monkeypatch):
    now = [0.0]
    sleeps = []

    monkeypatch.setattr(operator_module.time, "monotonic", lambda: now[0])

    def sleep(duration):
        sleeps.append(duration)
        now[0] += duration

    monkeypatch.setattr(operator_module.time, "sleep", sleep)
    return now, sleeps


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_teardown_waits_for_descendants_to_disappear_after_sigkill(monkeypatch):
    _, sleeps = _install_fake_teardown_clock(monkeypatch)
    signals = []
    kill_checks = 0

    def signal_group(_process_group, signal_number):
        signals.append(signal_number)
        return True

    def group_has_live_members(_process_group):
        nonlocal kill_checks
        if signals and signals[-1] == signal.SIGKILL:
            kill_checks += 1
            return kill_checks == 1
        return True

    monkeypatch.setattr(operator_module, "_signal_coordinator_group", signal_group)
    monkeypatch.setattr(
        operator_module,
        "_coordinator_group_has_live_members",
        group_has_live_members,
    )
    monkeypatch.setattr(operator_module, "close_job", lambda _job: None)

    result = operator_module._teardown_process_group(
        cast(Any, _ExitedProcess()),
        None,
        term_grace=0.0,
        kill_grace=0.1,
    )

    assert result.quiesced
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert sleeps


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_teardown_fails_closed_when_descendants_outlive_deadline(monkeypatch):
    now, _ = _install_fake_teardown_clock(monkeypatch)
    monkeypatch.setattr(
        operator_module,
        "_signal_coordinator_group",
        lambda _process_group, _signal_number: True,
    )
    monkeypatch.setattr(
        operator_module,
        "_coordinator_group_has_live_members",
        lambda _process_group: True,
    )
    monkeypatch.setattr(operator_module, "close_job", lambda _job: None)

    result = operator_module._teardown_process_group(
        cast(Any, _ExitedProcess()),
        None,
        term_grace=0.0,
        kill_grace=0.05,
    )

    assert not result.quiesced
    assert now[0] == pytest.approx(0.05)


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


@pytest.mark.parametrize(
    ("event", "error_match"),
    [
        (["not", "an", "event"], "event must be a dict"),
        ({"type": "running", "timestamp": 1}, "unexpected preparation event type"),
        (
            {"type": "node_started", "node_id": "read_1", "timestamp": 1},
            "unexpected preparation event type",
        ),
        ({"type": "terminal", "status": "success"}, "unexpected preparation event type"),
    ],
    ids=["non-dict", "running", "node", "terminal"],
)
def test_malformed_preparation_event_rolls_back_start(
    tmp_path, monkeypatch, event, error_match
):
    workflow = _write_standalone(tmp_path)
    operator = Operator([str(workflow)], watch=False, schedule=False)
    torn_down = []

    class FakeProcess:
        pid = 12345
        exitcode = None

        def __init__(self, *, args, **_kwargs):
            self.event_queue = args[8]

        def start(self):
            self.event_queue.put(event)

        def is_alive(self):
            return True

    class FakeContext:
        def Queue(self):  # noqa: N802 - mirrors multiprocessing context
            return _CloseableQueue()

        def Event(self):  # noqa: N802 - mirrors multiprocessing context
            return threading.Event()

        def Process(self, **kwargs):  # noqa: N802 - mirrors multiprocessing context
            return FakeProcess(**kwargs)

    monkeypatch.setattr(operator, "_mp", FakeContext())
    monkeypatch.setattr(operator_module, "assign_process", lambda *_args: None)
    monkeypatch.setattr(
        operator_module,
        "_teardown_process_group",
        lambda process, _job: torn_down.append(process),
    )
    try:
        with pytest.raises(RuntimeError, match=error_match):
            operator.start_run("flow")
        assert operator._runs == {}
        assert operator._active_runs == {}
        assert len(torn_down) == 1
    finally:
        operator.close()


def test_preparation_event_accepts_only_agent_invocation_field_schemas():
    field_schemas = (
        '{"inputs":[{"name":"question","type":"str","description":"Question"}],'
        '"outputs":[{"name":"answer","type":"str","description":"Answer"}]}'
    )
    event = {
        "type": "prepared",
        "node_ids": ["agent_1"],
        "graph": {"agent_1": []},
        "node_types": {"agent_1": "step"},
        "display_names": {"agent_1": "Agent"},
        "display_name": "Flow",
        "agent_field_schemas_json": {"agent_1": field_schemas},
    }

    assert operator_module._validate_preparation_event(event) == "prepared"

    event["agent_field_schemas_json"]["agent_1"] = (
        '{"inputs":[],"outputs":[],"instructions":"must not be retained"}'
    )
    with pytest.raises(
        operator_module._CoordinatorProtocolError,
        match="must contain only input and output schemas",
    ):
        operator_module._validate_preparation_event(event)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "unexpected"},
        {"type": "node_started"},
        {"type": "node_started", "node_id": "missing", "timestamp": 1},
        {"type": "terminal", "status": "unknown"},
        {"type": "terminal", "status": "cancelled", "payload": b"forbidden"},
        {
            "type": "terminal",
            "status": "success",
            "result_manifest_sha256": "0" * 64,
            "payload": b"forbidden",
        },
        {
            "type": "log",
            "timestamp": 10**5000,
            "level": 20,
            "node_id": "operator",
            "message": "hostile timestamp",
        },
        {"type": 10**5000},
        ["not", "an", "event"],
    ],
    ids=[
        "unknown-type",
        "missing-field",
        "unknown-node",
        "invalid-terminal-status",
        "cancelled-extra-payload",
        "success-extra-payload",
        "huge-timestamp",
        "huge-event-type",
        "non-dict",
    ],
)
def test_malformed_run_event_terminalizes_and_cleans_up(event):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_protocol_fault"
    run = RunState(run_id=run_id, flow_name="flow", status=RunStatus.RUNNING)
    run.nodes = {
        "running": NodeState(
            node_id="running", name="running", node_type="source", status=NodeStatus.RUNNING
        ),
        "pending": NodeState(node_id="pending", name="pending", node_type="dest"),
    }
    handle = _protocol_test_handle(operator._result_store.prepare())
    operator._runs[run_id] = run
    operator._active_runs[run_id] = handle
    logs = []
    operator.on_log(logs.append)
    updates = operator.subscribe_operator_updates()
    errors = []

    def drain():
        try:
            operator._drain_run_events(run_id, handle, [event])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=drain)
    thread.start()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert errors == []
    terminal = operator.get_run(run_id)
    assert terminal is not None
    assert terminal.status == RunStatus.FAILED
    assert terminal.ended_at is not None
    assert {node.status for node in terminal.nodes.values()} == {NodeStatus.SKIPPED}
    protocol_logs = [
        entry
        for entry in terminal.logs
        if entry.level == LogLevel.ERROR
        and entry.node_id == "operator"
        and entry.message.startswith("Malformed coordinator event")
    ]
    assert len(protocol_logs) == 1
    assert len(protocol_logs[0].message) <= 400
    assert logs == protocol_logs
    notifications = []
    while not updates.empty():
        notifications.append(updates.get_nowait().update.change)
    assert len(notifications) == 1
    assert isinstance(notifications[0], RunCreated)
    assert notifications[0].summary.status == RunStatus.FAILED
    assert run_id not in operator._active_runs
    assert handle.event_queue.closed
    operator.close()


def test_malformed_run_event_preserves_cancellation_precedence():
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_cancelled_protocol_fault"
    operator._runs[run_id] = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.RUNNING,
        nodes={"pending": NodeState("pending", "pending", "source")},
    )
    handle = _protocol_test_handle(
        operator._result_store.prepare(),
        cancelled=True,
    )
    operator._active_runs[run_id] = handle
    logs = []
    operator.on_log(logs.append)

    operator._drain_run_events(run_id, handle, [{"type": "terminal", "status": 3}])

    terminal = operator.get_run(run_id)
    assert terminal is not None
    assert terminal.status == RunStatus.CANCELLED
    assert terminal.ended_at is not None
    assert terminal.nodes["pending"].status == NodeStatus.SKIPPED
    assert len(logs) == 1
    assert logs[0].level == LogLevel.ERROR
    assert len(terminal.logs) == 1
    assert run_id not in operator._active_runs
    operator.close()


def test_malformed_event_for_unknown_run_exits_safely():
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_unknown"
    handle = _protocol_test_handle(operator._result_store.prepare())
    operator._active_runs[run_id] = handle

    operator._drain_run_events(run_id, handle, [None])

    assert run_id not in operator._active_runs
    assert handle.event_queue.closed
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
    updates = operator.subscribe_operator_updates()
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
            queued.append(updates.get_nowait().update.change)
        terminal_indexes = [
            index
            for index, change in enumerate(queued)
            if isinstance(change, RunStatusChanged) and change.status == RunStatus.CANCELLED
        ]
        assert terminal_indexes == [len(queued) - 2]
        assert queued[-1].status == NodeStatus.SKIPPED
    finally:
        operator.close()


def test_slow_update_consumer_receives_ordered_descriptors_and_detail_bodies(tmp_path):
    workflow = _write_standalone(
        tmp_path,
        body="log.info('first')\n    log.info('second')",
    )
    operator = Operator([str(workflow)], watch=False, schedule=False)
    subscription = operator.subscribe_operator_updates()
    details = []
    operator.on_detail_update(details.append)
    try:
        run_id = operator.start_run("flow")
        terminal = _wait_terminal(operator, run_id)
        assert terminal.status == RunStatus.SUCCESS

        changes = []
        while not subscription.empty():
            changes.append(subscription.get_nowait().update.change)
        statuses = [change.status for change in changes if isinstance(change, RunStatusChanged)]
        logs = [change for change in changes if isinstance(change, LogAppended)]
        assert statuses[0] == RunStatus.RUNNING
        assert statuses[-1] == RunStatus.SUCCESS
        assert [change.log.sequence for change in logs] == [1, 2]
        assert [
            detail.log.message.rsplit("] ", 1)[-1]
            for detail in details
            if isinstance(detail, LogDetailAppended)
        ] == ["first", "second"]
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
        with pytest.raises(TimeoutError, match="preparation exceeded"):
            operator.start_run("flow")
        assert time.monotonic() - started < 8.0
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


def test_watcher_refreshes_resource_derived_cron(tmp_path, caplog):
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
    caplog.set_level("INFO", logger="runtime.operator.operator")
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
    assert "Workflow watcher started" in caplog.text
    assert "Workflow reload started" in caplog.text
    assert "Workflow reload succeeded" in caplog.text
    assert "Workflow watcher stopped" in caplog.text


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
