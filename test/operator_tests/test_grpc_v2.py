"""Integration tests for the native V2 gRPC servicer."""

from __future__ import annotations

import os
import socket
import time

import grpc
import pytest

from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.server import serve

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")
TERMINAL_STATUSES = {"success", "failed", "cancelled"}


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def v2_server():
    op = Operator(
        workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
        schedule=False,
        watch=False,
    )
    port = _unused_port()
    server = serve(op, port=port, block=False)
    time.sleep(0.2)
    yield op, port
    server.stop(grace=1)
    op.close()


@pytest.fixture
def stub(v2_server):
    _, port = v2_server
    channel = grpc.insecure_channel(f"localhost:{port}")
    try:
        yield pb_grpc.OperatorServiceV2Stub(channel)
    finally:
        channel.close()


def _selector(stub) -> str:
    flows = stub.DiscoverFlows(pb.DiscoverFlowsRequestV2())
    return next(f for f in flows.flows if f.display_name == "simple_workflow").workflow_selector


def _await_terminal(stub, run_id: str, timeout: float = 30.0) -> pb.RunSnapshotV2:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run_id))
        if snapshot.summary.status in TERMINAL_STATUSES:
            return snapshot
        time.sleep(0.2)
    raise AssertionError(f"Run {run_id} did not reach a terminal status")


def test_discover_flows_returns_enriched_flow_list(stub):
    page = stub.DiscoverFlows(pb.DiscoverFlowsRequestV2())

    assert page.cursor.stream == "flows"
    assert page.cursor.stream_generation != 0
    assert page.cursor.topology_fingerprint
    simple = next(f for f in page.flows if f.display_name == "simple_workflow")
    assert simple.workflow_selector
    assert simple.manifest_digest
    assert simple.topology.node_ids
    assert simple.cron == "*/5 * * * *"
    assert page.scan_targets


def test_start_run_requires_idempotency_key(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.StartRun(pb.StartRunRequestV2(workflow_selector="simple_workflow"))
    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_start_run_rejects_staged_attachments(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_v2_staged",
                workflow_selector="simple_workflow",
                input_files=[
                    pb.FileAttachmentV2(
                        field_name="document",
                        object_uri="s3://bucket/key",
                        object_key="key",
                    )
                ],
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_run_lifecycle_snapshot_activity_and_result(stub):
    run_id = f"run_v2_{int(time.time() * 1000)}"
    stub.StartRun(pb.StartRunRequestV2(run_id=run_id, workflow_selector=_selector(stub)))

    snapshot = _await_terminal(stub, run_id)
    assert snapshot.summary.status == "success"
    assert snapshot.summary.run_id == run_id
    assert len(snapshot.nodes) == 3
    assert snapshot.topology.node_ids
    assert snapshot.cursor.stream_generation != 0

    page = stub.ListRunActivity(pb.ListRunActivityRequestV2(run_id=run_id, page_size=50))
    assert page.activities
    assert all(item.kind == "log" for item in page.activities)
    bodies = []
    for item in page.activities:
        assert item.detail_ref.object_uri.startswith("local://detail/")
        chunks = stub.ReadActivityDetail(
            pb.ReadActivityDetailRequestV2(detail_ref=item.detail_ref)
        )
        bodies.append(b"".join(chunk.data for chunk in chunks))
    assert any(b"Connecting to source database" in body for body in bodies)

    result = stub.GetRunResult(pb.GetRunResultRequestV2(run_id=run_id))
    assert result.run_id == run_id
    assert result.value.size_bytes == len(result.value.value_json.encode("utf-8"))

    node_page = stub.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id=run_id, node_id=snapshot.nodes[0].node_id)
    )
    assert node_page.run_id == run_id


def test_list_run_summaries_pages_with_continuation(stub):
    first = stub.ListRunSummaries(pb.ListRunSummariesRequestV2(page_size=1))
    assert len(first.runs) == 1
    if first.next_page.continuation_id:
        second = stub.ListRunSummaries(
            pb.ListRunSummariesRequestV2(page_size=1, continuation=first.next_page)
        )
        assert second.runs[0].run_id != first.runs[0].run_id


def test_watch_run_status_streams_run_lifecycle(stub):
    responses = stub.WatchRunStatus(pb.WatchRunStatusRequestV2())
    run_id = f"run_v2_watch_{int(time.time() * 1000)}"
    stub.StartRun(pb.StartRunRequestV2(run_id=run_id, workflow_selector=_selector(stub)))

    seen_created = False
    deadline = time.time() + 30.0
    for envelope in responses:
        assert envelope.cursor.stream_generation != 0
        if envelope.HasField("run_created") and envelope.run_created.summary.run_id == run_id:
            # Runs are published in the requesting state; nodes and topology
            # hydrate through GetRunSnapshot once the run is prepared.
            seen_created = True
        if (
            envelope.HasField("run_status_changed")
            and envelope.run_status_changed.summary.run_id == run_id
            and envelope.run_status_changed.summary.status in TERMINAL_STATUSES
        ):
            responses.cancel()
            break
        if time.time() > deadline:
            responses.cancel()
            raise AssertionError("watch did not observe a terminal status")
    assert seen_created


def test_watch_run_status_rejects_foreign_generation(stub):
    responses = stub.WatchRunStatus(
        pb.WatchRunStatusRequestV2(
            after_cursor=pb.LifecycleCursorV2(stream_generation=123, source_sequence=1)
        )
    )
    envelope = next(responses)
    assert envelope.HasField("reset_required")
    responses.cancel()


def test_foreign_scope_is_denied(stub):
    with pytest.raises(grpc.RpcError) as excinfo:
        stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id="run_v2_any",
                continuation=pb.ContinuationRefV2(
                    scope_ref=pb.ScopeReferenceV2(reference="other-scope"),
                    continuation_id="token",
                ),
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    with pytest.raises(grpc.RpcError) as excinfo:
        list(
            stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(
                    detail_ref=pb.ActivityDetailRefV2(
                        scope_ref=pb.ScopeReferenceV2(reference="other-scope"),
                        object_uri="local://detail/token",
                        object_key="token",
                    )
                )
            )
        )
    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED
