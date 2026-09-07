from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

from runtime.operator import Operator
from runtime.operator.server import serve

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
    operator = Operator([str(workflow_file)], watch=False, schedule=False)
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
