from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from runtime.operator import Operator
from runtime.operator.client import GrpcStateProvider
from runtime.operator.models import NodeStatus, RunState, RunStatus
from runtime.operator.operator import OPERATOR_RESTARTED_REASON
from runtime.operator.server import serve
from runtime.operator.state import OperatorStateStore
from runtime.operator.state.runs import RunsTable

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_core_workflow_runs_without_importing_agent_dependencies():
    script = """
import importlib.abc
import sys

class BlockAgentPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] in {'dspy', 'predict_rlm'}:
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockAgentPackages())
from avalanche import *

@source
def value():
    return 21

@step
def double(number):
    return number * 2

@workflow
def example():
    return double(value())

assert example().run(executor=LocalExecutor()).result() == 42
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_cli_runs_workflow_and_downloads_result_over_grpc(tmp_path):
    workflow_file = tmp_path / "flow.py"
    workflow_file.write_text(
        """
import avalanche as ava

class Input(ava.BaseInput):
    message: str
    document: ava.File
    workspace: ava.Workspace

class Context(ava.RunContext):
    request_id: str

@ava.source
def read(payload: Input, ctx: Context):
    return {
        'message': payload.message,
        'document': payload.document,
        'workspace': payload.workspace,
        'request_id': ctx.request_id,
    }

@ava.workflow(input=Input, context=Context)
def echo():
    return read()
"""
    )
    document = tmp_path / "document.bin"
    document.write_bytes(b"\x00\xffdocument")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_text("report")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    operator = Operator([str(workflow_file)], watch=False, schedule=False, state_db=":memory:")
    server = serve(operator, port=port, block=False)
    address = f"localhost:{port}"
    command = [sys.executable, "-m", "ava_cli"]
    try:
        started = subprocess.run(
            [
                *command,
                "run",
                "echo",
                "--connect",
                address,
                "--input",
                '{"message":"hello"}',
                "--context",
                '{"request_id":"request-1"}',
                "--file",
                f"document={document}",
                "--workspace",
                f"workspace={workspace}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert started.returncode == 0, started.stderr
        run_id = started.stdout.strip()
        output = tmp_path / "download"
        downloaded = subprocess.run(
            [
                *command,
                "result",
                run_id,
                "--connect",
                address,
                "--wait",
                "--timeout",
                "30",
                "--output-dir",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert downloaded.returncode == 0, downloaded.stderr
        metadata = json.loads(downloaded.stdout)
        assert metadata["result"]["message"] == "hello"
        assert metadata["result"]["request_id"] == "request-1"
        assert (output / metadata["files"][0]["path"]).read_bytes() == document.read_bytes()
        assert (
            output / metadata["workspaces"][0]["path"] / "report.txt"
        ).read_text() == "report"
        assert json.loads((output / metadata["metadata_path"]).read_text())["run_id"] == run_id

        for args in (
            ["run", "echo", "--input", "[]"],
            ["result", "missing-run", "--output-dir", str(tmp_path / "missing")],
        ):
            rejected = subprocess.run(
                [*command, *args, "--connect", address],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert rejected.returncode == 1
            assert rejected.stderr
            assert not rejected.stdout
        assert not (tmp_path / "missing").exists()
    finally:
        server.stop(grace=0).wait(timeout=5)
        operator.close()


_RESTART_FLOWS = """import time

import avalanche as ava


class Order(ava.BaseInput):
    label: str = "orders"


@ava.source
def summarize(order: Order):
    return {"label": order.label}


@ava.workflow(input=Order, cron="0 0 1 1 *")
def nightly():
    return summarize()


@ava.step
def wait_long():
    time.sleep(600)
    return "done"


@ava.workflow
def long_flow():
    return wait_long()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ava(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ava_cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _start_operator(flow: Path, state_db: Path, port: int) -> subprocess.Popen[str]:
    operator = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ava_cli",
            "operator",
            str(flow),
            "--state-db",
            str(state_db),
            "--port",
            str(port),
            "--no-web",
        ],
        cwd=flow.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 120
    connect = ("--connect", f"localhost:{port}")
    try:
        while _ava("schedule", "list", *connect, cwd=flow.parent).returncode:
            assert operator.poll() is None, "operator exited during startup"
            assert time.monotonic() < deadline, "operator did not start"
            time.sleep(0.5)
    except BaseException:
        operator.kill()
        operator.wait(timeout=10)
        raise
    return operator


def _wait_for_run(port: int, run_id: str, done: Callable[[RunState], bool]) -> RunState:
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        deadline = time.monotonic() + 120
        while True:
            run = provider.get_run(run_id)
            assert run is not None
            if done(run):
                return run
            assert time.monotonic() < deadline, f"run {run_id} stayed {run.status}"
            time.sleep(0.2)
    finally:
        provider.close()


def _process_group_alive(group: int) -> bool:
    """Whether the group has a member that is not a zombie waiting to be reaped."""
    listed = subprocess.run(
        ["ps", "-e", "-o", "pgid=,stat="], capture_output=True, text=True, check=True
    )
    return any(
        int(member_group) == group and not state.startswith("Z")
        for member_group, state in (line.split() for line in listed.stdout.splitlines())
    )


def _only_run_process_group(operator_pid: int) -> int:
    """The process group of the one run coordinator left under the operator.

    Each coordinator starts its own group; a finished run's coordinator may take a
    moment to exit, so wait until only one remains.
    """
    operator_group = os.getpgid(operator_pid)
    deadline = time.monotonic() + 60
    while True:
        listed = subprocess.run(
            ["ps", "-o", "pgid=", "--ppid", str(operator_pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        groups = {int(group) for group in listed.stdout.split()} - {operator_group}
        if len(groups) == 1:
            return groups.pop()
        assert time.monotonic() < deadline, f"run process groups: {sorted(groups)}"
        time.sleep(0.2)


@pytest.mark.skipif(sys.platform != "linux", reason="reads process groups with Linux ps")
def test_operator_cli_keeps_runs_results_and_schedules_across_kill_9(tmp_path):
    flow = tmp_path / "flows.py"
    flow.write_text(_RESTART_FLOWS)
    state_db = tmp_path / "operator" / "state.db"
    port = _free_port()
    connect = ("--connect", f"localhost:{port}")
    operator = _start_operator(flow, state_db, port)
    try:
        started = _ava(
            "run", "nightly", *connect, "--input", '{"label":"manual"}', cwd=tmp_path
        )
        assert started.returncode == 0, started.stderr
        finished_id = started.stdout.strip()
        _wait_for_run(port, finished_id, lambda run: run.status is RunStatus.SUCCESS)

        schedules = json.loads(_ava("schedule", "list", *connect, cwd=tmp_path).stdout)
        (selector,) = [entry["selector"] for entry in schedules if entry["name"] == "nightly"]
        enabled = _ava(
            "schedule",
            "enable",
            selector,
            *connect,
            "--input",
            '{"label":"cron"}',
            cwd=tmp_path,
        )
        assert enabled.returncode == 0, enabled.stderr

        started = _ava("run", "long_flow", *connect, cwd=tmp_path)
        assert started.returncode == 0, started.stderr
        long_id = started.stdout.strip()
        _wait_for_run(
            port,
            long_id,
            lambda run: any(node.status is NodeStatus.RUNNING for node in run.nodes.values()),
        )
        coordinator_group = _only_run_process_group(operator.pid)

        operator.send_signal(signal.SIGKILL)
        operator.wait(timeout=10)
        assert _process_group_alive(coordinator_group)

        operator = _start_operator(flow, state_db, port)
        # Startup stops the leftover run processes before it serves anything.
        assert not _process_group_alive(coordinator_group)
        interrupted = _wait_for_run(port, long_id, lambda run: True)
        assert interrupted.status is RunStatus.FAILED
        assert interrupted.terminal_seal is not None
        assert interrupted.terminal_seal.reason == OPERATOR_RESTARTED_REASON
        assert {node.error for node in interrupted.nodes.values()} == {
            OPERATOR_RESTARTED_REASON
        }
        assert _wait_for_run(port, finished_id, lambda run: True).status is RunStatus.SUCCESS

        downloaded = _ava(
            "result", finished_id, *connect, "--output-dir", str(tmp_path / "out"), cwd=tmp_path
        )
        assert downloaded.returncode == 0, downloaded.stderr
        assert json.loads(downloaded.stdout)["result"] == {"label": "manual"}
        schedules = json.loads(_ava("schedule", "list", *connect, cwd=tmp_path).stdout)
        (schedule,) = [entry for entry in schedules if entry["selector"] == selector]
        assert schedule["enabled"] is True
        assert schedule["input"] == {"label": "cron"}

        held = _ava("state", "info", "--state-db", str(state_db), cwd=tmp_path)
        assert held.returncode == 1
        assert str(state_db) in held.stderr
    finally:
        operator.send_signal(signal.SIGINT)
        operator.wait(timeout=60)

    info = _ava("state", "info", "--state-db", str(state_db), cwd=tmp_path)
    assert info.returncode == 0, info.stderr
    pruned = _ava(
        "state", "prune", "--keep-last", "1", "--state-db", str(state_db), cwd=tmp_path
    )
    assert pruned.returncode == 0, pruned.stderr
    store = OperatorStateStore.open(state_db)
    try:
        with store.transaction() as connection:
            remaining = RunsTable.list_summaries(connection)
    finally:
        store.close()
    assert [run.run_id for run in remaining] == [long_id]
