"""Spawned coordinators, gRPC baseline/replay, and result bundles retain skips."""

import socket
import time
from pathlib import Path

import pytest

import avalanche as ava
from runtime.operator.client import GrpcStateProvider, StreamState
from runtime.operator.models import NodeStatus, RunStatus
from runtime.operator.operator import Operator
from runtime.operator.server import serve


@pytest.fixture
def transport():
    fixtures = Path(__file__).parents[1] / "fixtures" / "skipped_workflows.py"
    operator = Operator([str(fixtures)], watch=False, schedule=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = serve(operator, port=port, host="127.0.0.1", block=False)
    provider = GrpcStateProvider(f"127.0.0.1:{port}")
    try:
        yield operator, provider
    finally:
        provider.close()
        server.stop(grace=0).wait()
        operator.close()


def wait_for(provider, run_id, predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = provider.get_run(run_id)
        if run is not None and predicate(run):
            return run
        time.sleep(0.05)
    pytest.fail(f"Run {run_id} did not reach expected state")


def test_spawned_skip_is_successful_and_retains_state_and_result(transport):
    operator, provider = transport
    updates = []

    def recover(notice):
        baseline = provider.load_reset_baseline(notice)
        provider.acknowledge_stream_reset(
            baseline.generation, baseline.operator_instance_id, baseline.as_of_event_ulid,
        )

    provider.on_stream_reset(recover)
    provider.on_run_update(updates.append)
    provider.start_stream()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and provider.stream_state is not StreamState.LIVE:
        time.sleep(0.02)
    assert provider.stream_state is StreamState.LIVE
    run_id = provider.start_run("skipped_outcomes")
    run = wait_for(provider, run_id, lambda run: run.status == RunStatus.SUCCESS)
    node = run.nodes["optional_records_1"]
    assert node.status == NodeStatus.SKIPPED
    assert node.skip == ava.skip("No eligible records for this partition", {"partition": "today", "count": 0})
    assert node.error is None
    assert node.started_at is not None and node.ended_at >= node.started_at
    assert all(item.status == NodeStatus.SUCCESS for key, item in run.nodes.items() if key != node.node_id)
    assert provider.get_run_result(run_id) == (node.skip, "fan-in complete", "dependency satisfied")
    # The independently decoded baseline and the live/replayed client agree.
    snapshot = operator.get_latest_run_snapshot(
        run_id, operator_instance_id=run.operator_instance_id,
    )
    assert next(item for item in snapshot.nodes if item.node_id == node.node_id).skip == node.skip
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
        item.nodes.get(node.node_id) and item.nodes[node.node_id].skip == node.skip
        for item in updates
    ):
        time.sleep(0.02)
    assert any(item.nodes.get(node.node_id) and item.nodes[node.node_id].skip == node.skip for item in updates)


@pytest.mark.parametrize("workflow,terminal", [
    ("failed_dependency", RunStatus.FAILED),
    ("cancelled_dependency", RunStatus.CANCELLED),
])
def test_unfinished_dependency_skips_are_not_authored_outcomes(transport, workflow, terminal):
    _, provider = transport
    run_id = provider.start_run(workflow)
    if terminal == RunStatus.CANCELLED:
        wait_for(provider, run_id, lambda run: any(node.status == NodeStatus.RUNNING for node in run.nodes.values()))
        provider.cancel_run(run_id)
    run = wait_for(provider, run_id, lambda run: run.status == terminal)
    dependent = run.nodes["dependency_only_1"]
    assert dependent.status == NodeStatus.SKIPPED
    assert dependent.skip is None
    if terminal == RunStatus.FAILED:
        assert run.nodes["failure_1"].status == NodeStatus.FAILED
        assert run.nodes["failure_1"].error
