"""One real terminal path: discover, press run, and retrieve the published result."""

import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from runtime.operator.client import GrpcStateProvider, OperatorCallError
from runtime.operator.models import RunStatus


@pytest.mark.tmux
def test_terminal_runs_discovered_workflow(tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    workflow = tmp_path / "terminal_flow.py"
    workflow.write_text(
        "import avalanche as ava\n"
        '@ava.source\ndef value():\n    return {"answer": 42}\n'
        "@ava.workflow\ndef terminal_flow():\n    return value()\n"
    )
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        port = listener.getsockname()[1]
    session = f"ava-test-{uuid4().hex}"
    root = Path(__file__).parents[2]

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-L", session, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=root,
        ).stdout

    def screen():
        return tmux("capture-pane", "-t", f"{session}:.1", "-p")

    client = GrpcStateProvider(f"localhost:{port}")
    try:
        tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "200",
            "-y",
            "50",
            shlex.join(["uv", "run", "ava", "operator", str(workflow), "--port", str(port)]),
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                if client.list_workflows():
                    break
            except OperatorCallError:
                pass
            assert time.monotonic() < deadline, "Operator did not become ready"
            time.sleep(0.1)
        tmux(
            "split-window",
            "-h",
            "-t",
            session,
            shlex.join(["uv", "run", "ava", "tui", "--connect", f"localhost:{port}"]),
        )
        deadline = time.monotonic() + 20
        while "DAG" not in screen():
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        if "EXPLORER" not in screen():
            tmux("send-keys", "-t", f"{session}:.1", "Space", "e")
        while "terminal_flow" not in screen():
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        tmux("send-keys", "-t", f"{session}:.1", "Escape", "r")
        deadline = time.monotonic() + 20
        while True:
            runs = client.list_runs("terminal_flow")
            if runs and runs[0].status in {RunStatus.SUCCESS, RunStatus.FAILED}:
                break
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        assert runs[0].status == RunStatus.SUCCESS, screen()
        assert client.get_run_result(runs[0].run_id) == {"answer": 42}
    finally:
        client.close()
        subprocess.run(
            ["tmux", "-L", session, "kill-server"],
            capture_output=True,
            timeout=10,
        )
