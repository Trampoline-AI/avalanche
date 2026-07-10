from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "test" / "fixtures"


def test_package_imports_and_console_entrypoint_are_available():
    import avalanche as ava

    assert ava.__version__ == metadata.version("avalanche-ai")
    assert ava.workflow is not None
    assert ava.LocalExecutor is not None
    assert ava.IcebergNamespace is not None
    assert ava.LanceNamespace is not None

    console_scripts = metadata.entry_points(group="console_scripts")
    scripts = {entry.name: entry.value for entry in console_scripts}
    assert scripts["ava"] == "ava_cli:main"

def test_core_root_star_import_excludes_lazy_agent_symbols():
    """Core root imports do not resolve optional agent-only packages."""
    source_root = REPO_ROOT / "src"
    pythonpath = os.pathsep.join(
        filter(None, (str(source_root), os.environ.get("PYTHONPATH")))
    )
    script = """
import importlib.abc
import sys

BLOCKED = {"dspy", "predict_rlm"}
LAZY_AGENT_SYMBOLS = {
    "Agent",
    "AgentStepError",
    "AgentStepExecutionError",
    "Desc",
    "InputField",
    "Signature",
    "Skill",
    "agent_step",
    "configure_agent",
    "generate_signature",
    "skills",
}


class BlockOptionalAgentPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"blocked optional dependency: {fullname}")
        return None


sys.meta_path.insert(0, BlockOptionalAgentPackages())

import avalanche as ava

assert LAZY_AGENT_SYMBOLS.isdisjoint(ava.__all__)
namespace = {}
exec("from avalanche import *", namespace)
assert LAZY_AGENT_SYMBOLS.isdisjoint(namespace)
assert BLOCKED.isdisjoint(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "core-only root star import resolved an optional agent dependency\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

def test_operator_grpc_can_list_and_run_fixture_flow():
    from avalanche.operator import Operator
    from avalanche.operator.client import GrpcStateProvider
    from avalanche.operator.models import RunStatus
    from avalanche.operator.server import serve

    port = _free_port()
    operator = Operator(
        workflow_paths=[str(FIXTURES_DIR / "sample_workflows.py")],
        watch=False,
        schedule=False,
    )
    server = serve(operator, port=port, block=False)
    provider = GrpcStateProvider(f"localhost:{port}")

    try:
        _wait_for_ping(provider)
        flow_names = {flow.name for flow in provider.list_workflows()}
        assert {"simple_workflow", "slow_workflow"}.issubset(flow_names)

        run_id = provider.start_run("simple_workflow")
        run = _wait_for_run_status(provider, run_id, RunStatus.SUCCESS)
        assert run is not None
        assert run.flow_name == "simple_workflow"
        assert run.logs
        assert run_id in {existing.run_id for existing in provider.list_runs("simple_workflow")}
    finally:
        provider.close()
        server.stop(grace=0)


@pytest.mark.asyncio
async def test_tui_mounts_headlessly_with_mock_provider():
    from avalanche.tui.app import AvalancheApp
    from avalanche.tui.widgets.dag import DagWidget

    app = AvalancheApp()

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert app._screen is not None
        assert app.store.current_workflow is not None
        assert app._screen.query_one("#dag-panel", DagWidget)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_ping(provider, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if provider.ping():
            return
        time.sleep(0.05)
    raise AssertionError("operator did not become ready")


def _wait_for_run_status(provider, run_id: str, status, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = provider.get_run(run_id)
        if run and run.status == status:
            return run
        time.sleep(0.05)
    return provider.get_run(run_id)
