"""Integration tests for the native V2 gRPC servicer."""

from __future__ import annotations

import os
import socket
import time
from dataclasses import replace
from datetime import datetime

import grpc
import pytest

from avalanche.runtime import File
from runtime.operator.models import LogEntry, LogLevel, RunState, RunStatus, SequencedLogEntry
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.result_store import publish_workflow_result
from runtime.operator.results import encode_workflow_result
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


@pytest.fixture
def contract_stub():
    operator = Operator([], schedule=False, watch=False)
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    channel = grpc.insecure_channel(f"localhost:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield operator, pb_grpc.OperatorServiceV2Stub(channel)
    finally:
        channel.close()
        server.stop(grace=1)
        operator.close()


@pytest.fixture
def retained_cursor_stub():
    operator = Operator([], schedule=False, watch=False, stream_history_capacity=1)
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    channel = grpc.insecure_channel(f"localhost:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield operator, pb_grpc.OperatorServiceV2Stub(channel)
    finally:
        channel.close()
        server.stop(grace=1)
        operator.close()


class _NeverCancelled:
    def is_set(self) -> bool:
        return False


def _seed_run_with_logs(operator: Operator, run_id: str, messages: tuple[str, ...]) -> RunState:
    run = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.SUCCESS,
        workflow_id="flow.py::flow",
        workflow_display_name="flow",
    )
    entries = [
        SequencedLogEntry(
            sequence=index,
            entry=LogEntry(
                timestamp=datetime(2026, 8, 17, 12, 0, index),
                level=LogLevel.INFO,
                node_id="node",
                message=message,
            ),
            size_bytes=len(message.encode("utf-8")),
        )
        for index, message in enumerate(messages, start=1)
    ]
    run.latest_log_sequence = len(entries)
    with operator._lock:
        operator._runs[run_id] = run
        operator._logs[run_id] = entries
    operator._notify_run(run)
    return run


def _seed_result_file(operator: Operator, run_id: str) -> None:
    pending = operator._result_store.prepare()
    try:
        digest = publish_workflow_result(
            encode_workflow_result(File(name="result.txt", content=b"result file")),
            pending.descriptor,
            (pending.device, pending.inode),
            _NeverCancelled(),
        )
        stored = operator._result_store.accept(pending, digest)
    except (OSError, RuntimeError, TypeError, ValueError):
        operator._result_store.discard(pending)
        raise
    with operator._lock:
        operator._stored_results[run_id] = stored


def _assert_complete_cursor(cursor: pb.LifecycleCursorV2) -> None:
    assert cursor.stream
    assert cursor.topology_fingerprint
    assert cursor.stream_generation
    assert cursor.retained_floor


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


def _initial_event_cursor(stub) -> pb.LifecycleCursorV2:
    responses = stub.WatchRunStatus(pb.WatchRunStatusRequestV2())
    envelope = next(responses)
    try:
        assert envelope.HasField("reset_required")
        _assert_complete_cursor(envelope.cursor)
        cursor = pb.LifecycleCursorV2()
        cursor.CopyFrom(envelope.reset_required.latest_cursor)
        return cursor
    finally:
        responses.cancel()


def _watch_from_current(stub, operator: Operator):
    if operator.current_sequence == 0:
        return stub.WatchRunStatus(pb.WatchRunStatusRequestV2())
    return stub.WatchRunStatus(
        pb.WatchRunStatusRequestV2(after_cursor=_initial_event_cursor(stub))
    )


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


def test_watch_run_status_streams_run_lifecycle(stub, v2_server):
    operator, _ = v2_server
    responses = _watch_from_current(stub, operator)
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
    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_cancel_run_rejects_an_empty_run_id(contract_stub):
    _, service = contract_stub

    with pytest.raises(grpc.RpcError) as excinfo:
        service.CancelRun(pb.CancelRunRequestV2())

    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_activity_continuations_cannot_cross_run_boundaries(contract_stub):
    operator, service = contract_stub
    _seed_run_with_logs(operator, "run-a", ("a-one", "a-two"))
    _seed_run_with_logs(operator, "run-b", ("b-one", "b-two"))

    first_page = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="run-a", page_size=1)
    )
    assert first_page.next_page.continuation_id
    _assert_complete_cursor(first_page.next_page.cursor)

    with pytest.raises(grpc.RpcError) as category_error:
        service.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id="run-a",
                node_id="node",
                page_size=1,
                continuation=first_page.next_page,
            )
        )
    assert category_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    with pytest.raises(grpc.RpcError) as excinfo:
        service.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id="run-b",
                page_size=1,
                continuation=first_page.next_page,
            )
        )

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_activity_detail_refs_are_immutable_and_rederived_before_streaming(contract_stub):
    operator, service = contract_stub
    _seed_run_with_logs(operator, "run-detail", ("original body",))
    page = service.ListRunActivity(
        pb.ListRunActivityRequestV2(run_id="run-detail", page_size=10)
    )
    reference = page.activities[0].detail_ref
    _assert_complete_cursor(page.cursor)
    assert reference.run_id == "run-detail"
    assert reference.activity_id == "log:1"
    assert reference.run_sequence == 1
    assert reference.object_uri.startswith("local://detail/")
    assert reference.object_key
    assert len(reference.sha256) == 64
    assert reference.size_bytes == len(b"original body")

    body = b"".join(
        chunk.data
        for chunk in service.ReadActivityDetail(
            pb.ReadActivityDetailRequestV2(detail_ref=reference)
        )
    )
    assert body == b"original body"

    forged = pb.ActivityDetailRefV2()
    forged.CopyFrom(reference)
    forged.sha256 = "0" * 64
    with pytest.raises(grpc.RpcError) as forged_error:
        list(service.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=forged)))
    assert forged_error.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    with operator._lock:
        original = operator._logs["run-detail"][0]
        operator._logs["run-detail"][0] = replace(
            original,
            entry=replace(original.entry, message="rewritten body with a different size"),
        )

    with pytest.raises(grpc.RpcError) as stale_error:
        list(service.ReadActivityDetail(pb.ReadActivityDetailRequestV2(detail_ref=reference)))
    assert stale_error.value.code() == grpc.StatusCode.DATA_LOSS


def test_artifact_refs_are_immutable_and_reject_forged_bindings(contract_stub):
    operator, service = contract_stub
    _seed_run_with_logs(operator, "run-artifact", ())
    _seed_result_file(operator, "run-artifact")

    result = service.GetRunResult(pb.GetRunResultRequestV2(run_id="run-artifact"))
    reference = result.files[0].artifact_ref
    _assert_complete_cursor(result.cursor)
    assert reference.run_id == "run-artifact"
    assert reference.artifact_id
    assert reference.run_sequence
    assert reference.object_uri.startswith("local://result/run-artifact/")
    assert reference.object_key == f"run-artifact/{reference.artifact_id}"
    assert len(reference.sha256) == 64
    assert reference.size_bytes == len(b"result file")

    body = b"".join(
        chunk.data
        for chunk in service.ReadRunOutputArtifact(
            pb.ReadRunOutputArtifactRequestV2(artifact_ref=reference)
        )
    )
    assert body == b"result file"

    forged = pb.RunOutputArtifactRefV2()
    forged.CopyFrom(reference)
    forged.size_bytes += 1
    with pytest.raises(grpc.RpcError) as excinfo:
        list(service.ReadRunOutputArtifact(pb.ReadRunOutputArtifactRequestV2(artifact_ref=forged)))
    assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_watch_resumes_only_from_complete_server_issued_event_cursors(stub, v2_server):
    operator, _ = v2_server
    responses = _watch_from_current(stub, operator)
    run_id = f"run_v2_cursor_{int(time.time() * 1000)}"
    stub.StartRun(pb.StartRunRequestV2(run_id=run_id, workflow_selector=_selector(stub)))

    issued: pb.LifecycleCursorV2 | None = None
    deadline = time.monotonic() + 30.0
    for envelope in responses:
        if envelope.HasField("run_created") and envelope.run_created.summary.run_id == run_id:
            issued = pb.LifecycleCursorV2()
            issued.CopyFrom(envelope.cursor)
            responses.cancel()
            break
        if time.monotonic() > deadline:
            responses.cancel()
            raise AssertionError("watch did not issue a run-created event cursor")
    assert issued is not None
    _assert_complete_cursor(issued)
    assert issued.stream == "operator-events"

    resumed = stub.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=issued))
    resumed_envelope = next(resumed)
    try:
        assert not resumed_envelope.HasField("reset_required")
        _assert_complete_cursor(resumed_envelope.cursor)
    finally:
        resumed.cancel()

    for field in (
        "stream",
        "topology_fingerprint",
        "stream_generation",
        "retained_floor",
        "source_sequence",
    ):
        invalid = pb.LifecycleCursorV2()
        invalid.CopyFrom(issued)
        if field == "stream":
            invalid.stream = "flows"
        elif field == "topology_fingerprint":
            invalid.topology_fingerprint = "foreign-topology"
        elif field == "stream_generation":
            invalid.stream_generation += 1
        elif field == "retained_floor":
            invalid.retained_floor += 1
        else:
            invalid.source_sequence += 1_000_000
        rejected = stub.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=invalid))
        reset = next(rejected)
        try:
            assert reset.HasField("reset_required"), field
            _assert_complete_cursor(reset.cursor)
            _assert_complete_cursor(reset.reset_required.history_floor)
            _assert_complete_cursor(reset.reset_required.latest_cursor)
        finally:
            rejected.cancel()

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
    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_watch_rejects_an_issued_cursor_after_its_retained_floor_advances(
    retained_cursor_stub,
):
    operator, service = retained_cursor_stub
    _seed_run_with_logs(operator, "retained-a", ("one",))
    issued = _initial_event_cursor(service)

    _seed_run_with_logs(operator, "retained-b", ("two",))
    rejected = service.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=issued))
    reset = next(rejected)
    try:
        assert reset.HasField("reset_required")
        assert reset.reset_required.latest_cursor.retained_floor > issued.retained_floor
    finally:
        rejected.cancel()
