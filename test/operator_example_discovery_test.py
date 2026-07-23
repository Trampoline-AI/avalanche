from __future__ import annotations

import socket
import time
from pathlib import Path

from runtime.operator import Operator, WorkflowRegistry
from runtime.operator.client import GrpcStateProvider
from runtime.operator.server import serve

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
CANONICAL_FLOW_NAMES = [
    "complex_dag_workflow",
    "cursor_workflow",
    "document_file_workflow",
    "operator_demo_workflow",
    "stream_workflow",
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_registry_scan_of_examples_returns_only_canonical_flows(monkeypatch, tmp_path):
    monkeypatch.setenv("AVALANCHE_EXAMPLE_ROOT", str(tmp_path / "examples"))
    registry = WorkflowRegistry()

    registry.scan([str(EXAMPLES_DIR)])

    assert sorted(flow.name for flow in registry.list_workflows()) == CANONICAL_FLOW_NAMES


def test_operator_served_with_examples_exposes_canonical_flows_over_grpc(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVALANCHE_EXAMPLE_ROOT", str(tmp_path / "examples"))
    port = _free_port()
    operator = Operator([str(EXAMPLES_DIR)], watch=False, schedule=False)
    server = serve(operator, port=port, block=False)
    provider = GrpcStateProvider(f"localhost:{port}")

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not provider.ping():
            time.sleep(0.05)

        assert provider.ping()
        assert sorted(flow.name for flow in provider.list_workflows()) == CANONICAL_FLOW_NAMES
    finally:
        provider.close()
        server.stop(grace=0)
