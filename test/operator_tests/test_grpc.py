"""Tests for gRPC server + client roundtrip."""

import hashlib
import json
import os
import socket
import threading
import time
from concurrent import futures
from types import SimpleNamespace

import grpc
import pytest

from avalanche.runtime import File
from runtime.operator import Operator
from runtime.operator._grpc import MAX_GRPC_MESSAGE_BYTES
from runtime.operator.client import (
    GrpcStateProvider,
    OperatorCallError,
    StaleResetAcknowledgementError,
    StreamState,
    _DetailHydrationRaceError,
    _run_from_snapshot,
)
from runtime.operator.convert_v2 import (
    operator_update_envelope_from_v2,
    run_snapshot_to_v2,
    run_summary_to_v2,
    workflow_info_from_v2,
    workflow_info_to_v2,
)
from runtime.operator.models import (
    CatalogReloadRequired,
    CatalogSnapshot,
    NodeSnapshot,
    NodeState,
    NodeStatus,
    ResetBaseline,
    ResetBaselineCursor,
    RunSnapshot,
    RunState,
    RunStatus,
    RunSummary,
    StreamResetNotice,
    TraceDescriptor,
    WorkflowInfo,
)
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.results import (
    MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_RESULT_ATTACHMENTS,
    MAX_RESULT_TOTAL_BYTES,
)
from runtime.operator.server import serve


def _scope(instance: str = "operator-1") -> pb.ScopeReferenceV2:
    return pb.ScopeReferenceV2(reference=instance)


def _event_ulid(sequence: int) -> str:
    return f"{sequence:026X}"


def _cursor(sequence: int, *, stream: str = "operator-events") -> pb.LifecycleCursorV2:
    del stream
    stream = "operator-events"
    return pb.LifecycleCursorV2(
        stream=stream,
        topology_fingerprint="0" if stream == "flows" else f"{stream}-topology",
        stream_generation=1,
        retained_floor_event_ulid=_event_ulid(0),
        event_ulid=_event_ulid(sequence),
    )


def _project_summary_cursor(
    *,
    stream: str = "project-summaries",
    topology_fingerprint: str = "project-summary-topology",
    source_generation: str = "2026-08-19T14:57:00Z",
    retained_floor_sequence: int = 10,
    target_head_sequence: int = 20,
    checkpoint_watermark: int = 15,
    checkpoint_digest: str = "checkpoint-digest-1",
) -> pb.ProjectSummaryCursorV2:
    return pb.ProjectSummaryCursorV2(
        stream=stream,
        topology_fingerprint=topology_fingerprint,
        source_generation=source_generation,
        retained_floor_sequence=retained_floor_sequence,
        target_head_sequence=target_head_sequence,
        checkpoint_watermark=checkpoint_watermark,
        checkpoint_digest=checkpoint_digest,
    )


def _baseline_cursor(sequence: int, *, retained_floor: int = 0) -> ResetBaselineCursor:
    return ResetBaselineCursor(
        stream="operator-events",
        topology_fingerprint="operator-events-topology",
        stream_generation=1,
        retained_floor_event_ulid=_event_ulid(retained_floor),
        event_ulid=_event_ulid(sequence),
    )


def _activity_continuation(
    run_id: str,
    node_id: str,
    token: str,
    *,
    instance: str,
) -> pb.ContinuationRefV2:
    return pb.ContinuationRefV2(
        scope_ref=_scope(instance),
        continuation_id=token,
        cursor=_cursor(1, stream=f"activity:{run_id}:{node_id or 'logs'}"),
    )


def _trace_detail_ref(
    run_id: str,
    node_id: str,
    trace: TraceDescriptor,
    run_sequence: int,
    *,
    instance: str,
) -> pb.ActivityDetailRefV2:
    return pb.ActivityDetailRefV2(
        run_id=run_id,
        scope_ref=_scope(instance),
        activity_id=f"trace:{node_id}:{trace.revision}",
        run_sequence=run_sequence,
        object_uri=f"local://trace/{run_id}/{node_id}/{trace.revision}",
        object_key=f"{run_id}/{node_id}/{trace.revision}",
        sha256="0" * 64,
        size_bytes=trace.size_bytes,
    )


def _snapshot_msg(snapshot: RunSnapshot, *, instance: str = "operator-1") -> pb.RunSnapshotV2:
    cursor = _cursor(snapshot.as_of_sequence, stream=f"run:{snapshot.summary.run_id}")
    if snapshot.as_of_event_ulid:
        cursor.event_ulid = snapshot.as_of_event_ulid
    return run_snapshot_to_v2(
        snapshot,
        cursor=cursor,
        scope_ref=_scope(instance),
        trace_detail_ref_for=lambda run_id, node_id, trace, run_sequence: _trace_detail_ref(
            run_id,
            node_id,
            trace,
            run_sequence,
            instance=instance,
        ),
        activity_continuations={
            node.node_id: _activity_continuation(
                snapshot.summary.run_id,
                node.node_id,
                node.event_page_token,
                instance=instance,
            )
            for node in snapshot.nodes
            if node.event_page_token
        },
        log_continuation=(
            _activity_continuation(
                snapshot.summary.run_id,
                "",
                snapshot.log_page_token,
                instance=instance,
            )
            if snapshot.log_page_token
            else None
        ),
    )


def _run_created_envelope(
    sequence: int,
    run_id: str,
    *,
    instance: str = "operator-1",
    status: str = "running",
) -> pb.RunStatusEnvelopeV2:
    return pb.RunStatusEnvelopeV2(
        event_ulid=_event_ulid(sequence),
        cursor=_cursor(sequence),
        scope_ref=_scope(instance),
        run_created=pb.RunCreatedV2(
            summary=pb.RunSummaryV2(
                run_id=run_id,
                workflow_selector="flow",
                workflow_display_name="flow",
                status=status,
                created_sequence=sequence,
                revision=sequence,
            )
        ),
    )


def _event_handle() -> SimpleNamespace:
    return SimpleNamespace(
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
    )


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


def _unused_port():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def _wait_for_run_success(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
            return run
        time.sleep(0.05)
    return client.get_run(run_id)


def test_latest_run_snapshot_client_returns_typed_snapshot_without_mutating_baseline():
    latest = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=0,
        as_of_event_ulid=_event_ulid(8),
        summary=RunSummary(
            run_id="run-selected",
            flow_name="flow",
            workflow_id="flow",
            workflow_display_name="flow",
            status=RunStatus.RUNNING,
            created_sequence=2,
            revision=8,
        ),
    )

    class LatestSnapshotStub:
        def __init__(self):
            self.request = None

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.request = request
            return _snapshot_msg(latest)

    provider = GrpcStateProvider("localhost:1")
    stub = LatestSnapshotStub()
    provider._stub = stub
    retained = RunState(
        run_id="run-retained",
        flow_name="flow",
        operator_instance_id="operator-1",
    )
    provider._install_structural_baseline(
        "operator-1", _event_ulid(4), {retained.run_id: retained}
    )
    try:
        snapshot = provider.get_latest_run_snapshot("run-selected", "operator-1")
    finally:
        provider.close()

    assert isinstance(snapshot, RunSnapshot)
    assert snapshot == latest
    assert stub.request.run_id == "run-selected"
    assert provider._cursor.operator_instance_id == "operator-1"
    assert provider._cursor.event_ulid == _event_ulid(4)
    assert set(provider._runs_by_id) == {"run-retained"}


def test_latest_run_snapshot_client_preserves_operator_epoch_failure():
    class EpochFailure(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.FAILED_PRECONDITION

        def details(self):
            return "operator instance changed"

    class RestartedStub:
        def __init__(self):
            self.request = None

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.request = request
            raise EpochFailure()

    provider = GrpcStateProvider("localhost:1")
    stub = RestartedStub()
    provider._stub = stub
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.get_latest_run_snapshot("run-selected", "operator-old")
    finally:
        provider.close()

    assert error.value.status is grpc.StatusCode.FAILED_PRECONDITION
    assert error.value.details == "operator instance changed"
    assert stub.request.run_id == "run-selected"


@pytest.mark.parametrize(
    "value",
    [
        pb.ResultValueV2(
            value_json="{}",
            sha256=hashlib.sha256(b"{}").hexdigest(),
            size_bytes=1,
        ),
        pb.ResultValueV2(value_json="{}", sha256="0" * 64, size_bytes=2),
    ],
)
def test_get_run_result_rejects_tampered_v2_value_binding(value):
    class ResultStub:
        def GetRunResult(self, request, **kwargs):  # noqa: N802
            assert request.run_id == "run-result"
            return pb.RunResultV2(run_id="run-result", value=value)

    provider = GrpcStateProvider("localhost:1")
    provider._stub = ResultStub()
    try:
        with pytest.raises(ValueError, match="result value does not match"):
            provider.get_run_result("run-result")
    finally:
        provider.close()


def test_grpc_envelope_includes_bounded_worst_case_metadata_headroom():
    worst_case_metadata_bytes = MAX_RESULT_ATTACHMENTS * (
        4 * MAX_ATTACHMENT_NAME_LENGTH + 4 * MAX_ATTACHMENT_MEDIA_TYPE_LENGTH + 256
    )
    assert MAX_GRPC_MESSAGE_BYTES >= (MAX_RESULT_TOTAL_BYTES + worst_case_metadata_bytes)


def test_grpc_wire_limit_accepts_exact_edge_and_rejects_max_plus_one():
    options = (
        ("grpc.max_send_message_length", MAX_GRPC_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_GRPC_MESSAGE_BYTES),
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1), options=options)
    handler = grpc.unary_unary_rpc_method_handler(
        lambda _request, _context: b"",
        request_deserializer=lambda payload: payload,
        response_serializer=lambda payload: payload,
    )
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("limits.Service", {"Probe": handler}),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=options)
    probe = channel.unary_unary(
        "/limits.Service/Probe",
        request_serializer=lambda payload: payload,
        response_deserializer=lambda payload: payload,
    )
    try:
        assert probe(b"x" * MAX_GRPC_MESSAGE_BYTES, timeout=10) == b""
        with pytest.raises(grpc.RpcError) as exc_info:
            probe(b"x" * (MAX_GRPC_MESSAGE_BYTES + 1), timeout=10)
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        channel.close()
        server.stop(grace=0)


@pytest.fixture(scope="module")
def grpc_server():
    """Start a gRPC server with a real Operator for the test module."""
    op = Operator(
        workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
        schedule=False,
        watch=False,
    )
    port = _unused_port()
    server = serve(op, port=port, block=False)
    time.sleep(0.2)  # Let server bind
    yield op, server, port
    server.stop(grace=1)
    op.close()


@pytest.fixture
def client(grpc_server):
    """Create a GrpcStateProvider client connected to the test server."""
    _, _, port = grpc_server
    provider = GrpcStateProvider(f"localhost:{port}")
    yield provider
    provider.close()


class TestGrpcRoundtrip:
    def test_list_workflows(self, client):
        workflows = client.list_workflows()
        names = [p.name for p in workflows]
        assert "simple_workflow" in names
        assert "slow_workflow" in names

    def test_workflow_info_structure(self, client):
        workflows = client.list_workflows()
        info = next(p for p in workflows if p.name == "simple_workflow")
        assert len(info.node_ids) == 3
        assert "source" in info.node_types.values()
        assert info.file_path.endswith("sample_workflows.py")

    def test_start_run_and_complete(self, client):
        run_id = client.start_run("simple_workflow")
        assert run_id.startswith("run_")

        # Wait for completion
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get_run(run_id)
            if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.05)

        run = client.get_run(run_id)
        assert run.status == RunStatus.SUCCESS

    def test_rapid_starts_publish_requesting_runs_before_preparation(
        self, client, grpc_server, monkeypatch
    ):
        operator, _, _ = grpc_server
        release_preparation = threading.Event()
        await_prepared = operator._await_prepared

        def delay_preparation(handle):
            assert release_preparation.wait(timeout=5)
            return await_prepared(handle)

        monkeypatch.setattr(operator, "_await_prepared", delay_preparation)
        try:
            first_run_id = client.start_run("simple_workflow")
            second_run_id = client.start_run("simple_workflow")

            assert client.get_run(first_run_id).status is RunStatus.REQUESTING
            assert client.get_run(second_run_id).status is RunStatus.REQUESTING
        finally:
            release_preparation.set()

    def test_start_run_honors_client_run_id(self, client):
        requested_run_id = "run_client_owned"

        assert client.start_run("simple_workflow", run_id=requested_run_id) == requested_run_id

    def test_start_run_duplicate_custom_id_maps_to_already_exists(self, client):
        requested_run_id = "run_grpc_duplicate"
        assert client.start_run("simple_workflow", run_id=requested_run_id) == requested_run_id

        with pytest.raises(OperatorCallError) as error:
            client.start_run("simple_workflow", run_id=requested_run_id)
        assert error.value.status is grpc.StatusCode.ALREADY_EXISTS

    def test_start_run_rejects_oversized_caller_owned_run_id(self, client):
        with pytest.raises(OperatorCallError) as error:
            client.start_run("simple_workflow", run_id="r" * 257)

        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT
        assert "256-byte UTF-8 limit" in str(error.value)

    def test_start_run_context_json_cannot_forge_lineage_vector(self, tmp_path):
        workflow_path = tmp_path / "lineage_workflows.py"
        workflow_path.write_text(
            """
import logging

import avalanche as ava

log = logging.getLogger(__name__)


@ava.source(slug="capture")
def capture(ctx: ava.RunContext):
    log.info(
        "lineage=%s; run_id=%s; workflow=%s; executor=%s; node_id=%s; "
        "node_name=%s; node_slug=%s",
        ctx.lineage_vector,
        ctx.run_id,
        ctx.workflow_name,
        ctx.executor_type,
        ctx.node_id,
        ctx.node_name,
        ctx.node_slug,
    )


@ava.workflow
def lineage_context_workflow():
    capture()
""",
        )
        op = Operator(workflow_paths=[str(workflow_path)], schedule=False, watch=False)
        port = _unused_port()
        server = serve(op, port=port, block=False)
        time.sleep(0.2)
        provider = GrpcStateProvider(f"localhost:{port}")
        try:
            run_id = "run_grpc_real"
            response = provider._stub.StartRun(
                pb.StartRunRequestV2(
                    workflow_selector="lineage_context_workflow",
                    run_id=run_id,
                    context_json=json.dumps(
                        {
                            "run_id": "run_fake",
                            "workflow_name": "fake_workflow",
                            "executor_type": "fake_executor",
                            "node_id": "fake_node_1",
                            "node_name": "fake_node",
                            "node_slug": "fake-node",
                            "lineage_vector": {"upstream": "run_fake"},
                        }
                    ),
                )
            )

            assert response.run_id == run_id
            run = _wait_for_run_success(provider, run_id)
            assert run is not None
            assert run.status == RunStatus.SUCCESS
            messages = [item.message for item in run.logs]
            assert any("lineage={}" in message for message in messages)
            assert any(f"run_id={run_id}" in message for message in messages)
            assert any("workflow=lineage_context_workflow" in message for message in messages)
            assert any("executor=local" in message for message in messages)
            assert any("node_id=capture_1" in message for message in messages)
            assert any("node_name=capture" in message for message in messages)
            assert any("node_slug=capture" in message for message in messages)
            assert not any("run_fake" in message for message in messages)
            assert not any("fake_node" in message for message in messages)
        finally:
            provider.close()
            server.stop(grace=1)
            op.close()

    def test_start_run_rejects_bad_file_metadata_before_response(self, client):
        start_request = pb.StartRunRequestV2(
            workflow_selector="input_workflow",
            run_id="run_bad_inline_checksum",
            input_files=[
                pb.FileAttachmentV2(
                    field_name="document",
                    inline_bytes=b"contents",
                    sha256="0" * 64,
                )
            ],
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            client._stub.StartRun(start_request)

        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert client.get_run(start_request.run_id) is None

    def test_start_run_passes_json_context_and_file_attachments(self, client):
        run_id = client.start_run(
            "input_workflow",
            input={"message": "from-grpc"},
            context={"request_id": "req_grpc", "run_id": "spoofed_user_id"},
            files={"document": File(name="note.txt", content=b"grpc-bytes")},
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get_run(run_id)
            if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.05)

        run = client.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        messages = [item.message for item in run.logs]
        assert any("message=from-grpc" in message for message in messages)
        assert any("request_id=req_grpc" in message for message in messages)
        assert any(f"run_id={run_id}" in message for message in messages)
        assert not any("run_id=spoofed_user_id" in message for message in messages)
        assert any("file=grpc-bytes" in message for message in messages)

    def test_start_run_rejects_duplicate_top_level_input_fields(self, client):
        with pytest.raises(OperatorCallError) as error:
            client.start_run(
                "input_workflow",
                input={"message": "from-grpc", "document": "json-value"},
                files={"document": File(name="note.txt", content=b"grpc-bytes")},
            )

        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT
        assert "Duplicate input field 'document'" in str(error.value)

    def test_start_run_roundtrips_file_above_grpc_default_limit(self, tmp_path):
        workflow_path = tmp_path / "large_file_workflow.py"
        workflow_path.write_text(
            """
import avalanche as ava


class LargeFileInput(ava.BaseInput):
    document: ava.File


@ava.source
def measure(payload: LargeFileInput, log=ava.Logger()):
    log.info(f"file_size={len(payload.document.content)}; sha256={payload.document.sha256}")


@ava.workflow(input=LargeFileInput)
def large_file_workflow():
    measure()
""",
        )
        operator = Operator(
            workflow_paths=[str(workflow_path)],
            schedule=False,
            watch=False,
        )
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        content = b"x" * (4 * 1024 * 1024 + 1)
        try:
            file = File(name="large.bin", content=content)
            run_id = provider.start_run(
                "large_file_workflow",
                files={"document": file},
            )

            assert run_id
            run = _wait_for_run_success(provider, run_id)
            assert run is not None
            assert run.status == RunStatus.SUCCESS
            assert any(
                f"file_size={len(content)}; sha256={file.sha256}" in entry.message
                for entry in run.logs
            )
        finally:
            provider.close()
            server.stop(grace=1)
            operator.close()

    def test_list_runs(self, client):
        run_id = client.start_run("simple_workflow")
        time.sleep(0.5)  # Let it complete

        runs = client.list_runs("simple_workflow")
        ids = [r.run_id for r in runs]
        assert run_id in ids

    def test_cancel_run(self, client):
        run_id = client.start_run("slow_workflow")
        time.sleep(0.2)  # Let it start
        client.cancel_run(run_id)

        # Cancellation may already be terminal by the time this read completes.
        run = client.get_run(run_id)
        assert run.status in {RunStatus.REQUESTING, RunStatus.RUNNING, RunStatus.CANCELLED}
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            run = client.get_run(run_id)
            if run.status == RunStatus.CANCELLED:
                break
            time.sleep(0.05)
        assert run.status == RunStatus.CANCELLED

    def test_get_unknown_run_returns_none(self, client):
        run = client.get_run("nonexistent")
        assert run is None

    def test_stream_run_updates(self, client):
        """StreamRunUpdates should materialize live state changes."""
        updates = []

        def on_update(run):
            updates.append((run.run_id, run.status))

        client.on_run_update(on_update)

        def recover_stream(notice):
            baseline = client.load_reset_baseline(notice)
            client.acknowledge_stream_reset(
                baseline.generation,
                baseline.operator_instance_id,
                baseline.as_of_event_ulid,
            )

        client.on_stream_reset(recover_stream)
        client.start_stream()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and client.stream_state is not StreamState.LIVE:
            time.sleep(0.05)
        assert client.stream_state is StreamState.LIVE

        run_id = client.start_run("simple_workflow")
        completion_deadline = time.monotonic() + 20

        while time.monotonic() < completion_deadline:
            if any(rid == run_id and s == RunStatus.RUNNING for rid, s in updates):
                break
            time.sleep(0.05)

        statuses = [s for rid, s in updates if rid == run_id]
        assert RunStatus.REQUESTING in statuses
        assert RunStatus.RUNNING in statuses

    def test_workflow_selector_request_starts(self, client):
        response = client._stub.StartRun(
            pb.StartRunRequestV2(
                workflow_selector="simple_workflow", run_id="run_selector_request"
            )
        )
        assert response.run_id == "run_selector_request"


def test_proto_identity_roundtrip_and_absolute_path_redaction(tmp_path):
    info = WorkflowInfo(
        name="shared",
        display_name="Shared report",
        workflow_id="root/reports/daily.py::shared",
        root_alias="root",
        relative_file="reports/daily.py",
        builder_symbol="shared",
        file_path=str(tmp_path / "reports" / "daily.py"),
        node_ids=["load_1"],
        graph={"load_1": []},
        node_types={"load_1": "source"},
    )

    message = workflow_info_to_v2(info)
    assert message.file_path == "reports/daily.py"
    assert not os.path.isabs(message.file_path)
    restored = workflow_info_from_v2(message)
    assert restored.workflow_id == info.workflow_id
    assert restored.display_name == info.display_name
    assert restored.file_path == "reports/daily.py"


def test_ping_success_does_not_heal_failed_update_stream():
    provider = GrpcStateProvider("localhost:1")

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "stream offline"

    class SplitHealthStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            raise Unavailable()

        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            return pb.FlowListV2()

    provider._stub = SplitHealthStub()
    try:
        provider.on_run_update(lambda _run: None)
        provider.start_stream()
        deadline = time.monotonic() + 1.0
        while provider.stream_state is not StreamState.FAILED and time.monotonic() < deadline:
            time.sleep(0.005)

        assert provider.stream_state is StreamState.FAILED
        assert provider.operator_reachable is False
        assert provider.ping() is True
        assert provider.operator_reachable is True
        assert provider.stream_state is StreamState.FAILED
        assert provider.stream_error == "UNAVAILABLE: stream offline"
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("status", "reachable"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, True),
        (grpc.StatusCode.ALREADY_EXISTS, True),
        (grpc.StatusCode.FAILED_PRECONDITION, True),
        (grpc.StatusCode.UNAUTHENTICATED, True),
        (grpc.StatusCode.UNAVAILABLE, False),
        (grpc.StatusCode.DEADLINE_EXCEEDED, False),
    ],
)
def test_ping_classifies_application_errors_separately_from_transport_failures(
    status, reachable
):
    provider = GrpcStateProvider("localhost:1")

    class RpcFailure(grpc.RpcError):
        def code(self):
            return status

        def details(self):
            return "operation failed"

    class ErrorStub:
        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            raise RpcFailure()

    provider._stub = ErrorStub()
    provider.operator_reachable = not reachable
    try:
        assert provider.ping() is False
        assert provider.operator_reachable is reachable
        assert provider.last_error == f"{status.name}: operation failed"
    finally:
        provider.close()


def _trace_health_provider(stub) -> GrpcStateProvider:
    trace_data = b'{"complete":true}'
    descriptor = TraceDescriptor(
        status="completed",
        revision=3,
        available=True,
        complete=True,
        size_bytes=len(trace_data),
    )
    run = RunState(
        run_id="run-trace-health",
        flow_name="flow",
        operator_instance_id="operator-1",
        created_sequence=1,
        revision=3,
        nodes={
            "agent": NodeState(
                node_id="agent",
                name="agent",
                node_type="step",
                trace=descriptor,
                revision=3,
            )
        },
    )
    provider = GrpcStateProvider("localhost:1")
    provider._stub = stub
    provider._runs_by_id[run.run_id] = run
    reference = pb.ActivityDetailRefV2(
        run_id=run.run_id,
        scope_ref=_scope("operator-1"),
        activity_id="trace:agent:3",
        run_sequence=run.created_sequence,
        object_uri=f"local://trace/{run.run_id}/agent/3",
        object_key=f"{run.run_id}/agent/3",
        sha256=hashlib.sha256(trace_data).hexdigest(),
        size_bytes=len(trace_data),
    )
    provider._trace_detail_refs[(run.run_id, "agent", descriptor.revision)] = reference
    provider.get_run = lambda run_id: run if run_id == run.run_id else None
    return provider


def test_read_trace_success_records_reachability_without_healing_update_stream():
    class SuccessfulStub:
        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            return iter(
                [
                    pb.ActivityDetailChunkV2(
                        chunk_index=0,
                        data=b'{"complete":true}',
                        eof=True,
                    )
                ]
            )

    provider = _trace_health_provider(SuccessfulStub())
    provider.operator_reachable = False
    provider.stream_state = StreamState.FAILED
    provider.stream_error = "UNAVAILABLE: live updates interrupted"
    try:
        detail = provider.hydrate_trace("run-trace-health", "agent")
        assert detail is not None
        assert detail.trace_body == {"complete": True, "steps": []}
        assert provider.operator_reachable is True
        assert provider.stream_state is StreamState.FAILED
        assert provider.stream_error == "UNAVAILABLE: live updates interrupted"
    finally:
        provider.close()


def test_trace_cache_evicts_oldest_bodies_across_unique_run_node_keys():
    trace_data = b"{}"
    descriptor = TraceDescriptor(
        status="completed",
        revision=4,
        available=True,
        complete=True,
        size_bytes=len(trace_data),
    )
    runs = {
        f"run-{index}": RunState(
            run_id=f"run-{index}",
            flow_name="flow",
            operator_instance_id="operator-1",
            created_sequence=index + 1,
            revision=4,
            nodes={
                "agent": NodeState(
                    node_id="agent",
                    name="agent",
                    node_type="agent",
                    trace=descriptor,
                    revision=4,
                )
            },
        )
        for index in range(4)
    }

    class TraceStub:
        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            return iter(
                [
                    pb.ActivityDetailChunkV2(
                        chunk_index=0,
                        data=trace_data,
                        eof=True,
                    )
                ]
            )

    provider = GrpcStateProvider(
        "localhost:1",
        max_detail_body_bytes=2,
        max_retained_detail_count=2,
        max_retained_detail_bytes=4,
    )
    provider._stub = TraceStub()
    provider._install_structural_baseline("operator-1", _event_ulid(4), runs)
    for run in runs.values():
        reference = pb.ActivityDetailRefV2(
            run_id=run.run_id,
            scope_ref=_scope("operator-1"),
            activity_id="trace:agent:4",
            run_sequence=run.created_sequence,
            object_uri=f"local://trace/{run.run_id}/agent/4",
            object_key=f"{run.run_id}/agent/4",
            sha256=hashlib.sha256(trace_data).hexdigest(),
            size_bytes=len(trace_data),
        )
        provider._trace_detail_refs[(run.run_id, "agent", descriptor.revision)] = reference
    provider.get_run = lambda run_id: runs.get(run_id)
    try:
        for run_id in runs:
            detail = provider.hydrate_trace(run_id, "agent")
            assert detail is not None
            assert detail.trace_body == {"steps": []}
            assert provider._retained_detail_count <= 2
            assert provider._retained_detail_bytes <= 4
            assert len(provider._detail_cache_usage) <= 2

        retained = [provider._trace_cache_key(run_id, "agent") for run_id in runs]
        assert list(provider._detail_cache_usage) == retained[-2:]
        assert set(provider._trace_bodies) == {
            ("run-2", "agent"),
            ("run-3", "agent"),
        }
        assert provider._retained_detail_count == 2
        assert provider._retained_detail_bytes == 4
    finally:
        provider.close()


def test_detail_hydration_rejects_advertised_and_streamed_bytes_before_accumulating():
    class OversizedStream:
        def __init__(self):
            self.cancelled = False

        def __iter__(self):
            return iter([pb.ActivityDetailChunkV2(chunk_index=0, data=b"four", eof=True)])

        def cancel(self):
            self.cancelled = True

    class DetailStub:
        def __init__(self):
            self.calls = 0
            self.stream = OversizedStream()

        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            self.calls += 1
            return self.stream

    stub = DetailStub()
    provider = GrpcStateProvider("localhost:1", max_detail_body_bytes=4)
    provider._stub = stub
    provider._detail_refs_by_key["token"] = pb.ActivityDetailRefV2(
        run_id="run-1",
        scope_ref=_scope("operator-1"),
        activity_id="log:3",
        run_sequence=3,
        object_uri="local://detail/token",
        object_key="token",
        sha256="0" * 64,
        size_bytes=3,
    )
    try:
        with pytest.raises(_DetailHydrationRaceError, match="configured hydration"):
            provider._read_detail_body("token", 5)
        assert stub.calls == 0

        with pytest.raises(_DetailHydrationRaceError, match="advertised size"):
            provider._read_detail_body("token", 3)
        assert stub.calls == 1
        assert stub.stream.cancelled is True
    finally:
        provider.close()


def test_detail_reads_reuse_the_complete_server_issued_reference():
    body = b"body"
    reference = pb.ActivityDetailRefV2(
        run_id="run-1",
        scope_ref=_scope("operator-1"),
        activity_id="log:3",
        run_sequence=3,
        object_uri="local://detail/token",
        object_key="token",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )

    class DetailStub:
        def __init__(self):
            self.reference: pb.ActivityDetailRefV2 | None = None

        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            self.reference = pb.ActivityDetailRefV2()
            self.reference.CopyFrom(request.detail_ref)
            return iter([pb.ActivityDetailChunkV2(chunk_index=0, data=body, eof=True)])

    provider = GrpcStateProvider("localhost:1")
    stub = DetailStub()
    provider._stub = stub
    try:
        provider._remember_detail_reference(reference)
        assert provider._read_detail_body("token", len(body), scope="operator-1") == body
    finally:
        provider.close()

    assert stub.reference == reference


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("max_retained_detail_count", True, TypeError),
        ("max_retained_detail_count", 1.5, TypeError),
        ("max_retained_detail_count", 0, ValueError),
        ("max_retained_detail_bytes", False, TypeError),
        ("max_retained_detail_bytes", "1", TypeError),
        ("max_retained_detail_bytes", -1, ValueError),
        ("max_paged_items", True, TypeError),
        ("max_paged_items", 1.5, TypeError),
        ("max_paged_items", 0, ValueError),
    ],
)
def test_retained_detail_limits_require_positive_integers(name, value, error):
    with pytest.raises(error, match=rf"{name} must be"):
        GrpcStateProvider("localhost:1", **{name: value})


@pytest.mark.parametrize(
    ("detail_kind", "max_count", "max_bytes", "limit_name"),
    [
        ("logs", 3, 100, "body count"),
        ("events", 100, 3, "byte"),
    ],
)
def test_multi_page_detail_hydration_fails_before_unbounded_or_partial_publication(
    detail_kind,
    max_count,
    max_bytes,
    limit_name,
):
    class PagedDetailStub:
        def __init__(self):
            self.body_reads = 0

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            return pb.RunSummaryPageV2(
                cursor=_cursor(4, stream="run-summaries"),
                scope_ref=_scope(),
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            node = pb.NodeSnapshotV2(
                node_id="node-1",
                name="Node",
                node_type="agent",
                status="running",
                revision=4,
            )
            if detail_kind == "events":
                node.trace.CopyFrom(
                    pb.TraceDescriptorV2(
                        status="in_progress",
                        revision=4,
                        event_count=4,
                        latest_event_sequence=4,
                    )
                )
                node.activity_continuation.CopyFrom(
                    pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="events",
                        cursor=_cursor(4, stream="activity:run-1:node-1"),
                    )
                )
            snapshot = pb.RunSnapshotV2(
                cursor=_cursor(4, stream="run:run-1"),
                scope_ref=_scope(),
                summary=pb.RunSummaryV2(
                    run_id="run-1",
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=4,
                ),
                nodes=[node],
                latest_log_sequence=4 if detail_kind == "logs" else 0,
            )
            if detail_kind == "logs":
                snapshot.log_continuation.CopyFrom(
                    pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="logs",
                        cursor=_cursor(4, stream="activity:run-1:logs"),
                    )
                )
            return snapshot

        @staticmethod
        def _position(token: str, prefix: str) -> int:
            if token == prefix:
                return 1
            return int(token.removeprefix(f"{prefix}-")) + 1

        def ListRunActivity(self, request, **kwargs):  # noqa: N802
            token = request.continuation.continuation_id
            if request.node_id:
                sequence = self._position(token, "events")
                activity = pb.RunActivityDescriptorV2(
                    activity_id=f"agent:node-1:{sequence}",
                    run_sequence=sequence,
                    kind="agent_event",
                    size_bytes=1,
                    node_id="node-1",
                    invocation_id="invocation",
                    detail_ref=pb.ActivityDetailRefV2(
                        run_id="run-1",
                        scope_ref=_scope(),
                        activity_id=f"agent:node-1:{sequence}",
                        run_sequence=sequence,
                        object_uri=f"local://detail/event-{sequence}",
                        object_key=f"event-{sequence}",
                        sha256=hashlib.sha256(b"x").hexdigest(),
                        size_bytes=1,
                    ),
                )
            else:
                sequence = self._position(token, "logs")
                activity = pb.RunActivityDescriptorV2(
                    activity_id=f"log:{sequence}",
                    run_sequence=sequence,
                    kind="log",
                    timestamp=float(sequence),
                    level="INFO",
                    size_bytes=1,
                    node_id="node-1",
                    detail_ref=pb.ActivityDetailRefV2(
                        run_id="run-1",
                        scope_ref=_scope(),
                        activity_id=f"log:{sequence}",
                        run_sequence=sequence,
                        object_uri=f"local://detail/log-{sequence}",
                        object_key=f"log-{sequence}",
                        sha256=hashlib.sha256(b"x").hexdigest(),
                        size_bytes=1,
                    ),
                )
            page = pb.RunActivityPageV2(
                cursor=_cursor(4, stream=f"activity:run-1:{request.node_id or 'logs'}"),
                run_id="run-1",
                activities=[activity],
                scope_ref=_scope(),
            )
            if sequence < 4:
                page.next_page.CopyFrom(
                    pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id=(
                            f"{'events' if request.node_id else 'logs'}-{sequence}"
                        ),
                        cursor=_cursor(
                            4,
                            stream=f"activity:run-1:{request.node_id or 'logs'}",
                        ),
                    )
                )
            return page

        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            self.body_reads += 1
            yield pb.ActivityDetailChunkV2(chunk_index=0, data=b"x", eof=True)

    stub = PagedDetailStub()
    provider = GrpcStateProvider(
        "localhost:1",
        max_detail_body_bytes=1,
        max_retained_detail_count=max_count,
        max_retained_detail_bytes=max_bytes,
    )
    provider._stub = stub
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.get_run("run-1")
        assert error.value.status is grpc.StatusCode.RESOURCE_EXHAUSTED
        assert limit_name in error.value.details
        assert stub.body_reads == 3
        assert provider._runs_by_id == {}
        assert provider._log_entries == {}
        assert provider._agent_events == {}
        assert provider._retained_detail_count == 0
        assert provider._retained_detail_bytes == 0
    finally:
        provider.close()


def test_run_summary_pagination_fails_before_unbounded_page_or_list_accumulation():
    class UnboundedSummaryStub:
        def __init__(self):
            self.calls = 0

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.calls += 1
            return pb.RunSummaryPageV2(
                cursor=_cursor(self.calls, stream="run-summaries"),
                scope_ref=_scope(),
                runs=[
                    pb.RunSummaryV2(
                        run_id=f"run-{self.calls}",
                        workflow_selector="flow",
                        workflow_display_name="flow",
                        status="running",
                        created_sequence=self.calls,
                        revision=self.calls,
                    )
                ],
                next_page=pb.ContinuationRefV2(
                    scope_ref=_scope(),
                    continuation_id=f"page-{self.calls}",
                    cursor=_cursor(self.calls, stream="run-summaries"),
                ),
            )

    stub = UnboundedSummaryStub()
    provider = GrpcStateProvider("localhost:1", max_paged_items=2)
    provider._stub = stub
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")
        assert error.value.status is grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "pagination item limit" in error.value.details
        assert stub.calls == 2
        assert provider._runs_by_id == {}
    finally:
        provider.close()


def test_trace_hydration_rejects_descriptor_above_configured_body_limit():
    class TraceStub:
        def __init__(self):
            self.calls = 0

        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            self.calls += 1
            return iter(())

    stub = TraceStub()
    provider = _trace_health_provider(stub)
    provider._max_detail_body_bytes = 4
    try:
        with pytest.raises(_DetailHydrationRaceError, match="configured hydration"):
            provider._hydrate_trace("run-trace-health", "agent")
        assert stub.calls == 0
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("status", "failure_phase", "reachable"),
    [
        (grpc.StatusCode.UNAVAILABLE, "creation", False),
        (grpc.StatusCode.UNAVAILABLE, "iteration", False),
        (grpc.StatusCode.INVALID_ARGUMENT, "creation", True),
        (grpc.StatusCode.INVALID_ARGUMENT, "iteration", True),
    ],
)
def test_read_trace_classifies_creation_and_iteration_failures(
    status, failure_phase, reachable
):
    class RpcFailure(grpc.RpcError):
        def code(self):
            return status

        def details(self):
            return "trace failed"

    class FailingStream:
        def __iter__(self):
            raise RpcFailure()

    class FailingStub:
        def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
            if failure_phase == "creation":
                raise RpcFailure()
            return FailingStream()

    provider = _trace_health_provider(FailingStub())
    provider.operator_reachable = not reachable
    provider.stream_state = StreamState.FAILED
    provider.stream_error = "existing stream failure"
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.hydrate_trace("run-trace-health", "agent")
        assert error.value.status is status
        assert provider.operator_reachable is reachable
        assert provider.last_error == f"{status.name}: trace failed"
        assert provider.stream_state is StreamState.FAILED
        assert provider.stream_error == "existing stream failure"
    finally:
        provider.close()


@pytest.mark.parametrize("status", [None, grpc.StatusCode.INVALID_ARGUMENT])
def test_unary_completion_after_close_cannot_overwrite_terminal_health(status):
    provider = GrpcStateProvider("localhost:1")
    entered = threading.Event()
    release = threading.Event()
    outcomes = []

    class RpcFailure(grpc.RpcError):
        def code(self):
            return status

        def details(self):
            return "late application error"

    class BlockingStub:
        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            entered.set()
            assert release.wait(timeout=1.0)
            if status is not None:
                raise RpcFailure()
            return pb.FlowListV2(cursor=_cursor(1), scope_ref=_scope())

    def invoke() -> None:
        try:
            outcomes.append(provider.list_workflows())
        except Exception as error:
            outcomes.append(error)

    provider._stub = BlockingStub()
    provider.operator_reachable = True
    provider.retry_count = 4
    provider.last_error = "before close"
    provider.stream_state = StreamState.LIVE
    thread = threading.Thread(target=invoke)
    thread.start()
    assert entered.wait(timeout=1.0)

    provider.close()
    release.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    if status is None:
        assert outcomes == [[]]
    else:
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], OperatorCallError)
        assert outcomes[0].status is status
    assert provider.operator_reachable is False
    assert provider.retry_count == 4
    assert provider.last_error == "before close"
    assert provider.stream_state is StreamState.STOPPED


def test_idle_accepted_stream_reaches_live_after_metadata_handshake():
    provider = GrpcStateProvider("localhost:1")
    metadata_read = threading.Event()
    iterating = threading.Event()
    release = threading.Event()

    class IdleStream:
        def initial_metadata(self):
            metadata_read.set()
            return ()

        def __iter__(self):
            return self

        def __next__(self):
            iterating.set()
            release.wait()
            raise StopIteration

    class IdleStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            return IdleStream()

    provider._stub = IdleStub()
    try:
        provider.on_run_update(lambda _run: None)
        provider.start_stream()
        assert metadata_read.wait(timeout=1.0)
        assert iterating.wait(timeout=1.0)
        assert provider.operator_reachable is True
        assert provider.stream_state is StreamState.LIVE
    finally:
        provider._stream_stop.set()
        release.set()
        provider.close()


def test_real_idle_server_stream_reaches_live_without_an_update():
    port = _unused_port()
    operator = Operator(
        workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
        schedule=False,
        watch=False,
    )
    server = serve(operator, port=port, block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    received = []
    try:
        provider.on_run_update(lambda run: received.append(run))
        provider.start_stream()
        deadline = time.monotonic() + 2.0
        while provider.stream_state is not StreamState.LIVE and time.monotonic() < deadline:
            time.sleep(0.01)

        assert provider.operator_reachable is True
        assert provider.stream_state is StreamState.LIVE
        assert received == []
    finally:
        provider.close()
        server.stop(grace=1)
        operator.close()


def test_post_header_stream_failures_preserve_exponential_reconnect_backoff():
    provider = GrpcStateProvider("localhost:1")
    delays = []

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "stream aborted after headers"

    class RecordingStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, delay):
            delays.append(delay)
            if len(delays) == 3:
                self.stopped = True
            return self.stopped

    class AcceptedThenUnavailable:
        def initial_metadata(self):
            return ()

        def __iter__(self):
            raise Unavailable()

    class FailingStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            return AcceptedThenUnavailable()

    provider._stream_stop = RecordingStop()
    provider._stub = FailingStub()
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert delays == [2, 4, 8]


def test_duplicate_only_streams_do_not_reset_reconnect_backoff_or_cursor():
    provider = GrpcStateProvider("localhost:1")
    delays = []
    requests: list[pb.LifecycleCursorV2] = []

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "duplicate replay aborted"

    class RecordingStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, delay):
            delays.append(delay)
            if len(delays) == 3:
                self.stopped = True
            return self.stopped

    duplicate = _run_created_envelope(99, "duplicate")

    class DuplicateThenUnavailable:
        def initial_metadata(self):
            return ()

        def __iter__(self):
            yield duplicate
            raise Unavailable()

    class DuplicateStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            cursor = pb.LifecycleCursorV2()
            cursor.CopyFrom(request.after_cursor)
            requests.append(cursor)
            return DuplicateThenUnavailable()

    provider._install_structural_baseline("operator-1", _event_ulid(99), {})
    provider._event_cursor = _cursor(99)
    provider._last_applied_update = operator_update_envelope_from_v2(duplicate).update
    provider._stream_stop = RecordingStop()
    provider._stub = DuplicateStub()
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert delays == [2, 4, 8]
    assert len(requests) == 3
    assert all(request == _cursor(99) for request in requests)
    assert provider._cursor.event_ulid == _event_ulid(99)


def test_client_accepts_a_monotonic_retention_floor_advance():
    provider = GrpcStateProvider("localhost:1")
    provider._install_structural_baseline("operator-1", _event_ulid(1), {})
    provider._event_cursor = _cursor(1)
    message = _run_created_envelope(2, "run-floor-advance")
    message.cursor.retained_floor_event_ulid = _event_ulid(1)
    try:
        provider._apply_update_envelope(
            operator_update_envelope_from_v2(message),
            event_cursor=message.cursor,
        )
        assert provider._cursor.event_ulid == _event_ulid(2)
        assert provider._event_cursor.retained_floor_event_ulid == _event_ulid(1)
    finally:
        provider.close()


def test_watch_accepts_an_older_floor_while_replay_remains_retained():
    operator = Operator(
        [],
        watch=False,
        schedule=False,
        stream_history_capacity=3,
    )
    server = None
    channel = None
    replay_stream = None
    try:
        for reloading in (False, True):
            operator._publish_workflow_reload_status(reloading=reloading)
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceV2Stub(channel)

        prior_cursor = stub.ListRunSummaries(pb.ListRunSummariesRequestV2(page_size=1)).cursor
        for reloading in (False, True, False):
            operator._publish_workflow_reload_status(reloading=reloading)

        replay_stream = stub.WatchRunStatus(
            pb.WatchRunStatusRequestV2(after_cursor=prior_cursor)
        )
        replayed = [next(replay_stream) for _ in range(3)]
        assert replayed[0].event_ulid < replayed[1].event_ulid < replayed[2].event_ulid
        assert all(
            message.WhichOneof("payload") == "flow_reload_status" for message in replayed
        )
        assert replayed[0].cursor.retained_floor_event_ulid != (
            prior_cursor.retained_floor_event_ulid
        )
        assert replayed[0].cursor.retained_floor_event_ulid > (
            prior_cursor.retained_floor_event_ulid
        )
        replay_stream.cancel()
        replay_stream = None

        operator._publish_workflow_reload_status(reloading=True)
        reset = next(stub.WatchRunStatus(pb.WatchRunStatusRequestV2(after_cursor=prior_cursor)))
        assert reset.WhichOneof("payload") == "reset_required"
    finally:
        if replay_stream is not None:
            replay_stream.cancel()
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_catalog_reload_notice_discover_flows_cursor_covers_notice_for_provider():
    operator = Operator([], watch=False, schedule=False)
    server = None
    provider = None
    stream = None
    try:
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        prior_cursor = provider._stub.DiscoverFlows(pb.DiscoverFlowsRequestV2()).cursor

        operator.publish_update(CatalogReloadRequired(deployment_id="deployment-a"))
        stream = provider._stub.WatchRunStatus(
            pb.WatchRunStatusRequestV2(after_cursor=prior_cursor)
        )
        message = next(stream)

        assert message.WhichOneof("payload") == "catalog_reload_required"
        assert message.catalog_reload_required.deployment_id == "deployment-a"
        assert message.scope_ref.reference == operator.operator_instance_id
        assert message.cursor.stream == "operator-events"
        assert message.cursor.event_ulid == message.event_ulid
        provider._apply_update_envelope(
            operator_update_envelope_from_v2(message),
            event_cursor=message.cursor,
        )
        assert provider._catalog.operator_instance_id == operator.operator_instance_id
        assert provider._catalog.as_of_event_ulid == message.event_ulid
    finally:
        if stream is not None:
            stream.cancel()
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


@pytest.mark.parametrize("changed_field", ["scope", "cursor", "revision"])
def test_provider_rejects_catalog_pages_that_change_baseline(changed_field):
    initial_catalog = CatalogSnapshot(
        operator_instance_id="operator-1",
        as_of_event_ulid=_event_ulid(4),
        revision=3,
    )

    class ChangingCatalogStub:
        def __init__(self):
            self.calls = 0

        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            del kwargs
            self.calls += 1
            if self.calls == 1:
                assert not request.continuation.continuation_id
                return pb.FlowListV2(
                    cursor=_cursor(5),
                    scope_ref=_scope(),
                    revision=4,
                    next_page=pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="flows:1",
                        cursor=_cursor(5),
                    ),
                )
            assert request.continuation.continuation_id == "flows:1"
            return pb.FlowListV2(
                cursor=_cursor(6 if changed_field == "cursor" else 5),
                scope_ref=_scope("operator-2" if changed_field == "scope" else "operator-1"),
                revision=5 if changed_field == "revision" else 4,
            )

    provider = GrpcStateProvider("localhost:1")
    stub = ChangingCatalogStub()
    provider._stub = stub
    provider._catalog = initial_catalog
    try:
        with pytest.raises(ValueError, match="scope, cursor, or revision"):
            provider.get_catalog()
    finally:
        provider.close()

    assert stub.calls == 2
    assert provider._catalog == initial_catalog


def test_authoritative_stream_progress_resets_reconnect_backoff():
    provider = GrpcStateProvider("localhost:1")
    delays = []

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "stream aborted"

    class RecordingStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, delay):
            delays.append(delay)
            if len(delays) == 2:
                self.stopped = True
            return self.stopped

    class ProgressThenUnavailable:
        def initial_metadata(self):
            return ()

        def __iter__(self):
            yield _run_created_envelope(1, "run-1")
            raise Unavailable()

    class ProgressStub:
        def __init__(self):
            self.calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            if self.calls == 1:
                raise Unavailable()
            return ProgressThenUnavailable()

    provider._stream_stop = RecordingStop()
    provider._stub = ProgressStub()
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert delays == [2, 1]
    assert provider._cursor.event_ulid == _event_ulid(1)


def test_stream_reconnect_transitions_through_replay_to_live():
    provider = GrpcStateProvider("localhost:1")
    observed = []

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "stream offline"

    class ImmediateRetryEvent:
        def __init__(self):
            self._event = threading.Event()

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self._event.set()

        def wait(self, _timeout=None):
            observed.append(provider.stream_state)
            return self._event.is_set()

    class ReconnectingStub:
        def __init__(self):
            self.calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            observed.append(provider.stream_state)
            if self.calls == 1:
                raise Unavailable()

            class ReplayStream:
                def initial_metadata(self):
                    observed.append(provider.stream_state)
                    return ()

                def __iter__(self):
                    observed.append(provider.stream_state)
                    yield _run_created_envelope(1, "run_live")
                    provider._stream_stop.set()

            return ReplayStream()

    provider._stream_stop = ImmediateRetryEvent()
    provider._stub = ReconnectingStub()
    provider._run_callbacks.append(lambda _run: None)
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert observed == [
        StreamState.CONNECTING,
        StreamState.FAILED,
        StreamState.CONNECTING,
        StreamState.REPLAYING,
        StreamState.LIVE,
    ]
    assert provider.operator_reachable is False
    assert provider.stream_state is StreamState.STOPPED


def test_client_list_runs_forwards_stable_project_summary_cursor_across_pages():
    summaries = [
        RunSummary(
            run_id="run-new",
            flow_name="flow",
            status=RunStatus.RUNNING,
            created_sequence=2,
            revision=2,
        ),
        RunSummary(
            run_id="run-old",
            flow_name="flow",
            status=RunStatus.SUCCESS,
            created_sequence=1,
            revision=1,
        ),
    ]
    summary_cursor = _project_summary_cursor()
    next_page = pb.ContinuationRefV2(
        scope_ref=_scope(),
        continuation_id="older",
        cursor=_cursor(2, stream="run-summaries"),
        project_summary_cursor=summary_cursor,
    )

    class PagedSummaryStub:
        def __init__(self):
            self.requests = []

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.requests.append(request)
            assert request.workflow_selector == "flow"
            if not request.continuation.continuation_id:
                return pb.RunSummaryPageV2(
                    cursor=_cursor(2, stream="run-summaries"),
                    scope_ref=_scope(),
                    runs=[run_summary_to_v2(summaries[0])],
                    next_page=next_page,
                    project_summary_cursor=summary_cursor,
                )
            assert request.continuation == next_page
            return pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
                runs=[run_summary_to_v2(summaries[1])],
                project_summary_cursor=summary_cursor,
            )

    provider = GrpcStateProvider("localhost:1")
    stub = PagedSummaryStub()
    provider._stub = stub
    try:
        runs = provider.list_runs("flow")
    finally:
        provider.close()

    assert [run.run_id for run in runs] == ["run-old", "run-new"]
    assert [request.workflow_selector for request in stub.requests] == ["flow", "flow"]


def test_client_list_runs_accepts_all_absent_project_summary_cursor_chain():
    class LegacySummaryStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            if not request.continuation.continuation_id:
                return pb.RunSummaryPageV2(
                    cursor=_cursor(2, stream="run-summaries"),
                    scope_ref=_scope(),
                    runs=[
                        pb.RunSummaryV2(
                            run_id="run-1",
                            status="running",
                            created_sequence=1,
                        )
                    ],
                    next_page=pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="next",
                        cursor=_cursor(2, stream="run-summaries"),
                    ),
                )
            return pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
                runs=[
                    pb.RunSummaryV2(
                        run_id="run-2",
                        status="running",
                        created_sequence=2,
                    )
                ],
            )

    provider = GrpcStateProvider("localhost:1")
    provider._stub = LegacySummaryStub()
    try:
        runs = provider.list_runs("flow")
    finally:
        provider.close()

    assert [run.run_id for run in runs] == ["run-1", "run-2"]


@pytest.mark.parametrize(
    ("page_has_cursor", "continuation_has_cursor"),
    [
        pytest.param(False, True, id="continuation-appears"),
        pytest.param(True, False, id="continuation-disappears"),
    ],
)
def test_client_list_runs_rejects_mixed_page_and_continuation_summary_cursors(
    page_has_cursor,
    continuation_has_cursor,
):
    summary_cursor = _project_summary_cursor()

    class MixedSummaryCursorStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            response = pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
                next_page=pb.ContinuationRefV2(
                    scope_ref=_scope(),
                    continuation_id="next",
                    cursor=_cursor(2, stream="run-summaries"),
                ),
            )
            if page_has_cursor:
                response.project_summary_cursor.CopyFrom(summary_cursor)
            if continuation_has_cursor:
                response.next_page.project_summary_cursor.CopyFrom(summary_cursor)
            return response

    provider = GrpcStateProvider("localhost:1")
    provider._stub = MixedSummaryCursorStub()
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")
    finally:
        provider.close()

    assert error.value.status is grpc.StatusCode.DATA_LOSS
    assert "continuation cursor" in error.value.details


@pytest.mark.parametrize(
    ("first_has_cursor", "second_has_cursor"),
    [
        pytest.param(False, True, id="cursor-appears-on-second-page"),
        pytest.param(True, False, id="cursor-disappears-on-second-page"),
    ],
)
def test_client_list_runs_rejects_summary_cursor_appearance_or_disappearance_across_pages(
    first_has_cursor,
    second_has_cursor,
):
    summary_cursor = _project_summary_cursor()

    class AppearingSummaryCursorStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            if not request.continuation.continuation_id:
                response = pb.RunSummaryPageV2(
                    cursor=_cursor(2, stream="run-summaries"),
                    scope_ref=_scope(),
                    next_page=pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="next",
                        cursor=_cursor(2, stream="run-summaries"),
                    ),
                )
                if first_has_cursor:
                    response.project_summary_cursor.CopyFrom(summary_cursor)
                    response.next_page.project_summary_cursor.CopyFrom(summary_cursor)
                return response
            response = pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
            )
            if second_has_cursor:
                response.project_summary_cursor.CopyFrom(summary_cursor)
            return response

    provider = GrpcStateProvider("localhost:1")
    provider._stub = AppearingSummaryCursorStub()
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")
    finally:
        provider.close()

    assert error.value.status is grpc.StatusCode.DATA_LOSS
    assert "appeared or disappeared" in error.value.details


@pytest.mark.parametrize(
    "changed_cursor",
    [
        pytest.param(_project_summary_cursor(stream="other-summary-stream"), id="stream"),
        pytest.param(
            _project_summary_cursor(topology_fingerprint="other-topology"), id="topology"
        ),
        pytest.param(
            _project_summary_cursor(source_generation="2026-08-19T15:00:00Z"), id="source"
        ),
        pytest.param(_project_summary_cursor(retained_floor_sequence=11), id="retained-floor"),
        pytest.param(_project_summary_cursor(target_head_sequence=21), id="target-head"),
        pytest.param(_project_summary_cursor(checkpoint_watermark=16), id="watermark"),
        pytest.param(_project_summary_cursor(checkpoint_digest="other-digest"), id="digest"),
    ],
)
def test_client_list_runs_rejects_each_changed_project_summary_cursor_field(changed_cursor):
    summary_cursor = _project_summary_cursor()

    class ChangedSummaryCursorStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            if not request.continuation.continuation_id:
                return pb.RunSummaryPageV2(
                    cursor=_cursor(2, stream="run-summaries"),
                    scope_ref=_scope(),
                    next_page=pb.ContinuationRefV2(
                        scope_ref=_scope(),
                        continuation_id="next",
                        cursor=_cursor(2, stream="run-summaries"),
                        project_summary_cursor=summary_cursor,
                    ),
                    project_summary_cursor=summary_cursor,
                )
            return pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
                project_summary_cursor=changed_cursor,
            )

    provider = GrpcStateProvider("localhost:1")
    provider._stub = ChangedSummaryCursorStub()
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")
    finally:
        provider.close()

    assert error.value.status is grpc.StatusCode.DATA_LOSS
    assert "project summary cursor changed" in error.value.details


def test_client_list_runs_rejects_repeated_summary_continuation_with_stable_cursor():
    summary_cursor = _project_summary_cursor()

    class RepeatedSummaryStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            return pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
                next_page=pb.ContinuationRefV2(
                    scope_ref=_scope(),
                    continuation_id="repeated",
                    cursor=_cursor(2, stream="run-summaries"),
                    project_summary_cursor=summary_cursor,
                ),
                project_summary_cursor=summary_cursor,
            )

    provider = GrpcStateProvider("localhost:1")
    provider._stub = RepeatedSummaryStub()
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")
    finally:
        provider.close()

    assert error.value.status is grpc.StatusCode.DATA_LOSS
    assert "repeated a page token" in error.value.details


def test_reset_baseline_retains_runs_for_removed_workflows():
    live_workflow = WorkflowInfo(
        name="removed",
        file_path="live.py",
        node_ids=[],
        graph={},
        node_types={},
        workflow_id="live.py::live",
    )
    removed_summary = RunSummary(
        run_id="run-removed",
        flow_name="removed",
        status=RunStatus.RUNNING,
        workflow_id="removed.py::removed",
        created_sequence=1,
        revision=1,
    )
    snapshot = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=2,
        summary=removed_summary,
    )

    grouped = GrpcStateProvider._group_snapshot_runs(
        (live_workflow,),
        [snapshot],
    )

    assert list(grouped) == [
        live_workflow.selector,
        removed_summary.workflow_id,
    ]
    assert [run.run_id for run in grouped[removed_summary.workflow_id]] == ["run-removed"]


@pytest.mark.parametrize("first_failure", ["transient", "mismatch", "evicted"])
def test_default_reset_loader_retries_and_builds_bounded_authoritative_baseline(
    first_failure,
):
    workflow = WorkflowInfo(
        name="flow",
        file_path="flow.py",
        node_ids=["step"],
        graph={"step": []},
        node_types={"step": "step"},
        display_names={"step": "Step"},
        workflow_id="flow.py::flow",
        display_name="Flow",
    )
    summaries = [
        RunSummary(
            run_id="run_1",
            flow_name="flow",
            status=RunStatus.SUCCESS,
            workflow_id=workflow.selector,
            workflow_display_name="Flow",
            created_sequence=1,
            revision=1,
        ),
        RunSummary(
            run_id="run_2",
            flow_name="flow",
            status=RunStatus.RUNNING,
            workflow_id=workflow.selector,
            workflow_display_name="Flow",
            created_sequence=2,
            revision=3,
        ),
    ]
    snapshots = {
        "run_1": RunSnapshot(
            operator_instance_id="operator-new",
            as_of_sequence=3,
            summary=summaries[0],
        ),
        "run_2": RunSnapshot(
            operator_instance_id="operator-new",
            as_of_sequence=3,
            summary=summaries[1],
            nodes=(
                NodeSnapshot(
                    node_id="step",
                    name="Step",
                    node_type="step",
                    status=NodeStatus.RUNNING,
                    revision=3,
                ),
            ),
        ),
    }
    summary_cursor = _project_summary_cursor()

    class Retryable(grpc.RpcError):
        def __init__(self, status):
            self._status = status

        def code(self):
            return self._status

        def details(self):
            return "baseline version temporarily unavailable"

    class BaselineStub:
        def __init__(self):
            self.summary_attempts = 0
            self.summary_calls = 0
            self.list_flows_calls = 0
            self.snapshot_calls = []

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.summary_calls += 1
            if not request.continuation.continuation_id:
                self.summary_attempts += 1
                if first_failure == "transient" and self.summary_attempts == 1:
                    raise Retryable(grpc.StatusCode.UNAVAILABLE)
                operator_id = (
                    "operator-old"
                    if first_failure == "mismatch" and self.summary_attempts == 1
                    else "operator-new"
                )
                return pb.RunSummaryPageV2(
                    cursor=_cursor(3, stream="run-summaries"),
                    scope_ref=_scope(operator_id),
                    runs=[run_summary_to_v2(summaries[0])],
                    next_page=pb.ContinuationRefV2(
                        scope_ref=_scope(operator_id),
                        continuation_id="page-2",
                        cursor=_cursor(3, stream="run-summaries"),
                        project_summary_cursor=summary_cursor,
                    ),
                    project_summary_cursor=summary_cursor,
                )
            assert request.continuation.continuation_id == "page-2"
            assert request.continuation.project_summary_cursor == summary_cursor
            return pb.RunSummaryPageV2(
                cursor=_cursor(3, stream="run-summaries"),
                scope_ref=_scope("operator-new"),
                runs=[run_summary_to_v2(summaries[1])],
                project_summary_cursor=summary_cursor,
            )

        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            self.list_flows_calls += 1
            return pb.FlowListV2(
                cursor=pb.LifecycleCursorV2(
                    stream="operator-events",
                    topology_fingerprint="1",
                    stream_generation=1,
                    retained_floor_event_ulid=_event_ulid(0),
                    event_ulid=_event_ulid(3),
                ),
                flows=[workflow_info_to_v2(workflow)],
                scope_ref=_scope("operator-new"),
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.snapshot_calls.append(request.run_id)
            if first_failure == "evicted" and len(self.snapshot_calls) == 1:
                raise Retryable(grpc.StatusCode.FAILED_PRECONDITION)
            return _snapshot_msg(snapshots[request.run_id], instance="operator-new")

    provider = GrpcStateProvider("localhost:1")
    stub = BaselineStub()
    provider._stub = stub
    try:
        baseline = provider.load_reset_baseline(
            StreamResetNotice(
                generation=7,
                previous_event_ulid=_event_ulid(99),
                observed_event_ulid=_event_ulid(2),
            )
        )
        assert provider._event_cursor is None
    finally:
        provider.close()

    assert baseline.generation == 7
    assert baseline.operator_instance_id == "operator-new"
    assert baseline.as_of_event_ulid == _event_ulid(3)
    assert baseline.cursor == _baseline_cursor(3)
    assert [item.selector for item in baseline.catalog.workflows] == [workflow.selector]
    assert [run.run_id for run in baseline.runs_by_workflow[workflow.selector]] == [
        "run_1",
        "run_2",
    ]
    assert baseline.runs_by_workflow[workflow.selector][1].nodes["step"].status is (
        NodeStatus.RUNNING
    )
    assert stub.summary_calls == (3 if first_failure == "transient" else 4)
    assert stub.snapshot_calls == (
        ["run_1", "run_1", "run_2"] if first_failure == "evicted" else ["run_1", "run_2"]
    )
    assert stub.list_flows_calls == 3


def test_default_reset_loader_uses_immutable_snapshot_while_updates_continue():
    workflow = WorkflowInfo(
        name="flow",
        file_path="flow.py",
        node_ids=[],
        graph={},
        node_types={},
        workflow_id="flow.py::flow",
    )
    summary = RunSummary(
        run_id="run_1",
        flow_name="flow",
        status=RunStatus.RUNNING,
        workflow_id=workflow.selector,
        created_sequence=1,
        revision=2,
    )
    snapshot = RunSnapshot(
        operator_instance_id="operator-live",
        as_of_sequence=2,
        summary=summary,
    )

    class AdvancingStub:
        def __init__(self):
            self.current_sequence = 2
            self.summary_calls = 0
            self.snapshot_requests = []

        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            return pb.FlowListV2(
                cursor=pb.LifecycleCursorV2(
                    stream="operator-events",
                    topology_fingerprint="1",
                    stream_generation=1,
                    retained_floor_event_ulid=_event_ulid(0),
                    event_ulid=_event_ulid(self.current_sequence),
                ),
                flows=[workflow_info_to_v2(workflow)],
                scope_ref=_scope("operator-live"),
            )

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.summary_calls += 1
            response = pb.RunSummaryPageV2(
                cursor=_cursor(self.current_sequence, stream="run-summaries"),
                scope_ref=_scope("operator-live"),
                runs=[run_summary_to_v2(summary)],
            )
            self.current_sequence += 1
            return response

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.snapshot_requests.append(request)
            self.current_sequence += 1
            return _snapshot_msg(snapshot, instance="operator-live")

    provider = GrpcStateProvider("localhost:1")
    stub = AdvancingStub()
    provider._stub = stub
    try:
        baseline = provider.load_reset_baseline(
            StreamResetNotice(
                generation=1,
                previous_event_ulid=_event_ulid(99),
                observed_event_ulid=_event_ulid(2),
            )
        )
    finally:
        provider.close()

    assert baseline.as_of_event_ulid == _event_ulid(2)
    assert stub.summary_calls == 1
    assert len(stub.snapshot_requests) == 1
    request = stub.snapshot_requests[0]
    assert request.run_id == summary.run_id


def test_restart_reset_rejects_stale_generation_and_rebinds_update_epoch():
    def load_baseline(notice):
        return ResetBaseline(
            generation=notice.generation,
            operator_instance_id="operator-restarted",
            as_of_event_ulid=_event_ulid(3),
            catalog=CatalogSnapshot(workflows=()),
            runs_by_workflow={},
            cursor=_baseline_cursor(3),
        )

    provider = GrpcStateProvider(
        "localhost:1",
        reset_baseline_loader=load_baseline,
    )
    reset_notices = []
    reset_observed = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    received = []

    reset_envelope = _run_created_envelope(
        2, "run_recovered", instance="operator-restarted", status="success"
    )

    class RestartedStream:
        def initial_metadata(self):
            return ()

        def __iter__(self):
            yield _run_created_envelope(4, "run_newer", instance="operator-restarted")
            waiting.set()
            release.wait()

    class RestartedStub:
        def __init__(self):
            self.calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            assert metadata is None
            if self.calls == 1:
                assert request.after_cursor.event_ulid == _event_ulid(99)
                return iter((reset_envelope,))
            assert request.after_cursor.event_ulid == _event_ulid(3)
            return RestartedStream()

    def on_reset(notice):
        reset_notices.append(notice)
        reset_observed.set()

    provider._stub = RestartedStub()
    provider._install_structural_baseline("operator-original", _event_ulid(99), {})
    provider._event_cursor = _cursor(99)
    provider.on_stream_reset(on_reset)
    provider._run_callbacks.append(lambda run: received.append(run.run_id))
    try:
        provider._ensure_stream()
        assert reset_observed.wait(timeout=1.0)
        notice = reset_notices[0]
        assert notice == StreamResetNotice(
            generation=1,
            previous_event_ulid=_event_ulid(99),
            observed_event_ulid=_event_ulid(2),
            operator_instance_id="operator-restarted",
        )
        assert provider.stream_state is StreamState.RESET_REQUIRED
        assert provider._cursor == provider._cursor.__class__(
            "operator-original", _event_ulid(99)
        )

        baseline = provider.load_reset_baseline(notice)
        assert baseline.operator_instance_id == "operator-restarted"
        assert baseline.as_of_event_ulid == _event_ulid(3)
        with pytest.raises(StaleResetAcknowledgementError):
            provider.acknowledge_stream_reset(
                generation=notice.generation + 1,
                operator_instance_id="operator-restarted",
                reconciled_event_ulid=_event_ulid(3),
            )
        provider.acknowledge_stream_reset(
            generation=notice.generation,
            reconciled_event_ulid=_event_ulid(3),
            operator_instance_id="operator-restarted",
        )
        assert waiting.wait(timeout=1.0)
        assert provider.stream_state is StreamState.LIVE
        assert provider._cursor.operator_instance_id == "operator-restarted"
        assert provider._cursor.event_ulid == _event_ulid(4)
        assert received == ["run_newer"]
    finally:
        provider._stream_stop.set()
        release.set()
        provider.close()


@pytest.mark.parametrize(
    ("operator_instance_id", "reconciled_event_ulid"),
    [
        ("operator-other", _event_ulid(3)),
        ("operator-restarted", _event_ulid(2)),
        ("operator-restarted", _event_ulid(4)),
    ],
)
def test_reset_acknowledgement_requires_exact_validated_baseline(
    operator_instance_id,
    reconciled_event_ulid,
):
    notice = StreamResetNotice(
        generation=1,
        previous_event_ulid=_event_ulid(99),
        observed_event_ulid=_event_ulid(2),
    )
    expected = ResetBaseline(
        generation=notice.generation,
        operator_instance_id="operator-restarted",
        as_of_event_ulid=_event_ulid(3),
        catalog=CatalogSnapshot(workflows=()),
        runs_by_workflow={},
        cursor=_baseline_cursor(3),
    )
    provider = GrpcStateProvider(
        "localhost:1",
        reset_baseline_loader=lambda _notice: expected,
    )
    provider._pending_reset = notice
    provider.stream_state = StreamState.RESET_REQUIRED
    try:
        with pytest.raises(
            StaleResetAcknowledgementError,
            match="has no validated baseline",
        ):
            provider.acknowledge_stream_reset(
                generation=notice.generation,
                operator_instance_id=expected.operator_instance_id,
                reconciled_event_ulid=expected.as_of_event_ulid,
            )
        assert provider.load_reset_baseline(notice) is expected
        with pytest.raises(
            ValueError,
            match="does not match the validated baseline",
        ):
            provider.acknowledge_stream_reset(
                generation=notice.generation,
                operator_instance_id=operator_instance_id,
                reconciled_event_ulid=reconciled_event_ulid,
            )
        assert provider._pending_reset is notice
        assert provider.stream_state is StreamState.RESET_REQUIRED

        provider.acknowledge_stream_reset(
            generation=notice.generation,
            operator_instance_id=expected.operator_instance_id,
            reconciled_event_ulid=expected.as_of_event_ulid,
        )
        assert provider.stream_state is StreamState.LIVE
    finally:
        provider.close()


@pytest.mark.parametrize("observed_event_ulid", [99, 100])
def test_update_epoch_change_requires_reset_at_equal_or_higher_sequence(
    observed_event_ulid,
):
    provider = GrpcStateProvider("localhost:1")
    reset_notices = []
    reset_observed = threading.Event()

    class RestartedStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            assert request.after_cursor.event_ulid == _event_ulid(99)
            assert metadata is None
            yield _run_created_envelope(
                observed_event_ulid, "run-restarted", instance="operator-restarted"
            )

    def on_reset(notice):
        reset_notices.append(notice)
        reset_observed.set()

    provider._stub = RestartedStub()
    provider._install_structural_baseline("operator-original", _event_ulid(99), {})
    provider._event_cursor = _cursor(99)
    provider.on_stream_reset(on_reset)
    try:
        provider._ensure_stream()
        assert reset_observed.wait(timeout=1.0)
        assert reset_notices == [
            StreamResetNotice(
                generation=1,
                previous_event_ulid=_event_ulid(99),
                observed_event_ulid=_event_ulid(observed_event_ulid),
                operator_instance_id="operator-restarted",
            )
        ]
        assert provider.stream_state is StreamState.RESET_REQUIRED
        assert provider._cursor.operator_instance_id == "operator-original"
        assert provider._cursor.event_ulid == _event_ulid(99)
    finally:
        provider.close()


def test_client_skips_duplicate_update_sequence_without_epoch_reset():
    provider = GrpcStateProvider("localhost:1")
    received = []

    class DuplicateFirstStub:
        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            assert request.after_cursor.event_ulid == _event_ulid(98)
            assert metadata is None
            for sequence, run_id in (
                (99, "duplicate"),
                (99, "duplicate"),
                (100, "run_live"),
            ):
                yield _run_created_envelope(sequence, run_id)
            provider._stream_stop.set()

    provider._stub = DuplicateFirstStub()
    provider._install_structural_baseline("operator-1", _event_ulid(98), {})
    provider._event_cursor = _cursor(98)
    provider._run_callbacks.append(lambda run: received.append(run.run_id))
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert received == ["duplicate", "run_live"]
    assert provider._cursor.event_ulid == _event_ulid(100)


def test_concurrent_start_stream_calls_start_one_stream_thread():
    provider = GrpcStateProvider("localhost:1")
    registration_count = 16
    barrier = threading.Barrier(registration_count + 1)
    stream_entered = threading.Event()
    release_stream = threading.Event()

    class BlockingStub:
        def __init__(self):
            self.calls = 0
            self.thread_ids = set()

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            self.thread_ids.add(threading.get_ident())

            class BlockingStream:
                def __iter__(self):
                    return self

                def __next__(self):
                    stream_entered.set()
                    release_stream.wait()
                    raise StopIteration

            return BlockingStream()

    stub = BlockingStub()
    provider._stub = stub

    def register():
        barrier.wait()
        provider.on_run_update(lambda _run: None)
        provider.start_stream()

    registrations = [threading.Thread(target=register) for _ in range(registration_count)]
    for registration in registrations:
        registration.start()
    barrier.wait()
    for registration in registrations:
        registration.join()

    assert stream_entered.wait(timeout=1.0)
    assert stub.calls == 1
    assert stub.thread_ids == {provider._stream_thread.ident}

    provider._stream_stop.set()
    release_stream.set()
    provider.close()
    assert provider._stream_thread is not None
    assert not provider._stream_thread.is_alive()


def test_concurrent_close_and_stream_start_leave_no_live_thread_or_calls():
    provider = GrpcStateProvider("localhost:1")
    participant_count = 16
    barrier = threading.Barrier(participant_count + 2)
    close_returned = threading.Event()

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "offline"

    class FailingStub:
        def __init__(self):
            self.calls = 0
            self.post_close_calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            if close_returned.is_set():
                self.post_close_calls += 1
            raise Unavailable()

    stub = FailingStub()
    provider._stub = stub

    def register():
        barrier.wait()
        provider.on_run_update(lambda _run: None)
        provider.start_stream()

    def close():
        barrier.wait()
        provider.close()
        close_returned.set()

    registrations = [threading.Thread(target=register) for _ in range(participant_count)]
    closer = threading.Thread(target=close)
    for registration in registrations:
        registration.start()
    closer.start()
    barrier.wait()
    for registration in registrations:
        registration.join()
    closer.join()

    thread = provider._stream_thread
    assert thread is None or not thread.is_alive()
    assert stub.calls <= 1
    assert stub.post_close_calls == 0


def test_client_close_stops_reconnect_thread_and_prevents_new_calls():
    provider = GrpcStateProvider("localhost:1")
    called = threading.Event()

    class Unavailable(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.UNAVAILABLE

        def details(self):
            return "offline"

    class FailingStub:
        def __init__(self):
            self.calls = 0

        def WatchRunStatus(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            called.set()
            raise Unavailable()

    stub = FailingStub()
    provider._stub = stub
    provider.operator_reachable = True
    provider.on_run_update(lambda _run: None)
    provider.start_stream()
    assert called.wait(timeout=1.0)

    provider.close()
    provider.close()
    calls_after_close = stub.calls
    provider.on_run_update(lambda _run: None)
    provider.start_stream()

    assert provider._stream_thread is not None
    assert not provider._stream_thread.is_alive()
    assert stub.calls == calls_after_close
    assert provider.operator_reachable is False
    assert provider.stream_state is StreamState.STOPPED


def test_canonical_client_requests_use_workflow_selector():
    canonical_id = "root/reports/daily.py::build_report"

    class CapturingStub:
        def __init__(self):
            self.start_request = None
            self.list_request = None

        def DiscoverFlows(self, request, **kwargs):  # noqa: N802
            return pb.FlowListV2(
                cursor=_cursor(1, stream="flows"),
                flows=[
                    pb.FlowInfoV2(
                        workflow_selector=canonical_id,
                        display_name="Daily report",
                        workflow_id=canonical_id,
                        file_path="reports/daily.py",
                    )
                ],
                scope_ref=_scope(),
            )

        def StartRun(self, request, **kwargs):  # noqa: N802
            self.start_request = request
            return pb.StartRunResponseV2(run_id="run_legacy")

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.list_request = request
            return pb.RunSummaryPageV2(
                cursor=_cursor(1, stream="run-summaries"),
                scope_ref=_scope(),
            )

    provider = GrpcStateProvider("localhost:1")
    stub = CapturingStub()
    provider._stub = stub
    try:
        workflows = provider.list_workflows()
        assert workflows[0].workflow_id == canonical_id

        assert provider.start_run(canonical_id) == "run_legacy"
        assert stub.start_request.workflow_selector == canonical_id

        assert provider.list_runs(canonical_id) == []
        assert stub.list_request.workflow_selector == canonical_id
        assert stub.list_request.page_size == 1000
    finally:
        provider.close()


def test_unary_client_calls_pass_finite_timeout():
    calls = []

    class CapturingStub:
        def _capture(self, name, timeout):
            calls.append((name, timeout))
            assert timeout is not None
            assert 0 < timeout < float("inf")

        def DiscoverFlows(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("list", timeout)
            return pb.FlowListV2(cursor=_cursor(1), scope_ref=_scope())

        def StartRun(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("start", timeout)
            return pb.StartRunResponseV2(run_id="run_1")

        def GetRunSnapshot(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("get", timeout)
            return pb.RunSnapshotV2(
                cursor=_cursor(1, stream=f"run:{request.run_id}"),
                scope_ref=_scope(),
                summary=pb.RunSummaryV2(
                    run_id=request.run_id,
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="pending",
                ),
            )

        def ListRunSummaries(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("cursor" if request.page_size == 1 else "runs", timeout)
            return pb.RunSummaryPageV2(
                cursor=_cursor(1, stream="run-summaries"),
                scope_ref=_scope(),
            )

        def CancelRun(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("cancel", timeout)
            return pb.CancelRunResponseV2(run_id=request.run_id)

    provider = GrpcStateProvider("localhost:1", unary_timeout=3.5)
    provider._stub = CapturingStub()
    try:
        provider.list_workflows()
        provider.start_run("flow")
        provider.get_run("run_1")
        provider.get_latest_run_snapshot("run_1", "operator-1")
        provider.list_runs("flow")
        provider.cancel_run("run_1")
    finally:
        provider.close()

    assert calls == [
        ("list", 3.5),
        ("start", 3.5),
        ("cursor", 3.5),
        ("get", 3.5),
        ("get", 3.5),
        ("runs", 3.5),
        ("cancel", 3.5),
    ]


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (None, TypeError, "real number"),
        (True, TypeError, "real number"),
        ("1", TypeError, "real number"),
        (0, ValueError, "positive and finite"),
        (-1, ValueError, "positive and finite"),
        (float("nan"), ValueError, "positive and finite"),
        (float("inf"), ValueError, "positive and finite"),
        (float("-inf"), ValueError, "positive and finite"),
    ],
)
def test_unary_timeout_must_be_a_positive_finite_real(value, error, message):
    with pytest.raises(error, match=message):
        GrpcStateProvider("localhost:1", unary_timeout=value)


def test_canonical_and_ambiguous_grpc_selection(tmp_path):
    roots = [tmp_path / "left", tmp_path / "right"]
    source = "import avalanche as ava\n" "@ava.workflow\n" "def shared():\n" "    return None\n"
    for root in roots:
        root.mkdir()
        (root / "flow.py").write_text(source)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    operator = Operator(
        workflow_paths=[str(root) for root in roots], schedule=False, watch=False
    )
    server = serve(operator, port=port, block=False)
    provider = GrpcStateProvider(f"localhost:{port}")
    try:
        workflows = provider.list_workflows()
        assert [item.display_name for item in workflows] == ["shared", "shared"]
        ids = [item.workflow_id for item in workflows]
        assert ids == ["left/flow.py::shared", "right/flow.py::shared"]
        assert all(not os.path.isabs(item.file_path) for item in workflows)

        run_id = provider.start_run(ids[0])
        assert run_id.startswith("run_")
        assert [run.run_id for run in provider.list_runs(ids[0])] == [run_id]
        assert provider.list_runs(ids[1]) == []

        with pytest.raises(OperatorCallError) as error:
            provider.start_run("shared")
        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT
        assert ids[0] in str(error.value)
        assert ids[1] in str(error.value)

        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("shared")
        assert error.value.status is grpc.StatusCode.INVALID_ARGUMENT

        (roots[0] / "flow.py").write_text("VALUE = 1\n")
        failed_run_id = provider.start_run(ids[0])
        failed_run = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            failed_run = provider.get_run(failed_run_id)
            if failed_run is not None and failed_run.status == RunStatus.FAILED:
                break
            time.sleep(0.05)
        assert failed_run is not None
        assert failed_run.status == RunStatus.FAILED
        assert any("preparation failed" in item.message.lower() for item in failed_run.logs)
    finally:
        provider.close()
        server.stop(grace=1)
        operator.close()


def test_blocking_server_closes_operator_in_finally(monkeypatch):
    import runtime.operator.server as server_module

    class StopResult:
        def wait(self, timeout):
            assert timeout == 2.0

    class FakeServer:
        def __init__(self):
            self.stop_calls = []
            self.bind_address = None

        def add_insecure_port(self, address):
            self.bind_address = address
            return 1

        def start(self):
            pass

        def wait_for_termination(self):
            raise KeyboardInterrupt

        def stop(self, grace):
            self.stop_calls.append(grace)
            return StopResult()

    class FakeOperator:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    grpc_options = None
    fake_server = FakeServer()
    operator = FakeOperator()

    def fake_grpc_server(_executor, *, options):
        nonlocal grpc_options
        grpc_options = options
        return fake_server

    monkeypatch.setattr(server_module.grpc, "server", fake_grpc_server)
    monkeypatch.setattr(
        server_module.pb_grpc,
        "add_OperatorServiceV2Servicer_to_server",
        lambda _servicer, _server: None,
    )

    assert server_module.serve(operator, port=0, block=True) is fake_server
    assert operator.closed
    assert fake_server.stop_calls
    assert fake_server.bind_address == "127.0.0.1:0"
    assert dict(grpc_options) == {
        "grpc.max_send_message_length": MAX_GRPC_MESSAGE_BYTES,
        "grpc.max_receive_message_length": MAX_GRPC_MESSAGE_BYTES,
    }


@pytest.mark.parametrize("failure_stage", ["setup", "bind"])
def test_server_setup_and_bind_failures_close_operator_storage(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    import runtime.operator.server as server_module

    class FakeServer:
        def __init__(self):
            self.stop_calls = []

        def add_insecure_port(self, _address):
            return 0 if failure_stage == "bind" else 1

        def start(self):
            pass

        def stop(self, grace):
            self.stop_calls.append(grace)

    fake_server = FakeServer()
    monkeypatch.setattr(
        server_module.grpc,
        "server",
        lambda _executor, *, options: fake_server,
    )

    def register(_servicer, _server):
        if failure_stage == "setup":
            raise RuntimeError("registration failed")

    monkeypatch.setattr(
        server_module.pb_grpc,
        "add_OperatorServiceV2Servicer_to_server",
        register,
    )
    operator = Operator(
        [],
        watch=False,
        schedule=False,
        result_storage_directory=tmp_path,
    )
    root = operator._result_store.root

    with pytest.raises(RuntimeError):
        server_module.serve(operator, port=7433, block=False)

    assert operator._closed
    assert not root.exists()
    assert fake_server.stop_calls == [0]


def test_server_non_loopback_binding_is_explicit_and_warned(monkeypatch, caplog):
    import runtime.operator.server as server_module

    class FakeServer:
        bind_address = None

        def add_insecure_port(self, address):
            self.bind_address = address
            return 1

        def start(self):
            pass

    fake_server = FakeServer()
    monkeypatch.setattr(
        server_module.grpc,
        "server",
        lambda _executor, *, options: fake_server,
    )
    monkeypatch.setattr(
        server_module.pb_grpc,
        "add_OperatorServiceV2Servicer_to_server",
        lambda _servicer, _server: None,
    )

    server_module.serve(object(), port=7433, block=False, host="0.0.0.0")

    assert fake_server.bind_address == "0.0.0.0:7433"
    assert "trusted and authenticated boundary" in caplog.text


def test_agent_fields_roundtrip_and_legacy_defaults():
    workflow = WorkflowInfo(
        name="agent-flow",
        file_path="flow.py",
        node_ids=["agent_1"],
        graph={"agent_1": []},
        node_types={"agent_1": "step"},
        agent_node_ids=["agent_1"],
        agent_metadata_json={"agent_1": '{"signature":{"name":"Inspect"}}'},
    )
    restored = workflow_info_from_v2(workflow_info_to_v2(workflow))
    assert restored.agent_node_ids == ["agent_1"]
    assert restored.agent_metadata_json == workflow.agent_metadata_json

    legacy = workflow_info_from_v2(pb.FlowInfoV2())
    assert legacy.agent_node_ids == []
    assert legacy.agent_metadata_json == {}


def _seed_hydration_run(operator, run_id: str) -> RunState:
    run = RunState(run_id=run_id, flow_name="flow")
    run.nodes["agent-1"] = NodeState(
        node_id="agent-1",
        name="Agent",
        node_type="step",
    )
    operator._runs[run_id] = run
    operator._notify_run(run)
    for sequence in range(1, 6):
        operator._apply_event(
            run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "agent-1",
                "event": {
                    "kind": "evidence",
                    "invocation_id": "test-invocation",
                    "sequence": sequence,
                    "event_kind": "code.executed",
                    "timestamp_ns": sequence,
                    "data": {"iteration": sequence},
                },
            },
        )
    operator._apply_event(
        run_id,
        _event_handle(),
        {
            "type": "agent_evidence",
            "node_id": "agent-1",
            "event": {
                "kind": "trace_finished",
                "invocation_id": "test-invocation",
                "trace": {
                    "status": "completed",
                    "evidence": {"run_id": run_id, "complete": True},
                    "payload": "x" * (2 * 1024 * 1024 + 17),
                },
            },
        },
    )
    return run


def test_grpc_latest_snapshot_and_newest_pages_preserve_status_contract():
    operator = Operator([], watch=False, schedule=False)
    server = None
    channel = None
    try:
        run = _seed_hydration_run(operator, "run-latest-page")
        baseline = operator.list_run_summaries(page_size=10)
        retained = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=baseline.operator_instance_id,
            as_of_sequence=baseline.as_of_sequence,
        )
        assert retained is not None
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {"type": "running", "timestamp": 1.0},
        )

        port = _unused_port()
        server = serve(operator, port=port, block=False)
        channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = pb_grpc.OperatorServiceV2Stub(channel)

        latest = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run.run_id))
        assert latest.summary.status == RunStatus.RUNNING.value
        assert latest.scope_ref.reference == operator.operator_instance_id
        assert retained is not None
        assert latest.cursor.event_ulid >= latest.cursor.retained_floor_event_ulid

        first_logs = stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                page_size=2,
                order=pb.PageOrderV2.PAGE_ORDER_V2_NEWEST_FIRST,
            )
        )
        assert [item.run_sequence for item in first_logs.activities] == sorted(
            (item.run_sequence for item in first_logs.activities),
            reverse=True,
        )
        assert all(item.kind == "log" for item in first_logs.activities)
        assert first_logs.next_page.continuation_id
        second_logs = stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                page_size=2,
                continuation=first_logs.next_page,
                order=pb.PageOrderV2.PAGE_ORDER_V2_NEWEST_FIRST,
            )
        )
        assert {item.run_sequence for item in first_logs.activities}.isdisjoint(
            item.run_sequence for item in second_logs.activities
        )

        first_events = stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run.run_id,
                node_id="agent-1",
                page_size=2,
                order=pb.PageOrderV2.PAGE_ORDER_V2_NEWEST_FIRST,
            )
        )
        assert [item.run_sequence for item in first_events.activities] == sorted(
            (item.run_sequence for item in first_events.activities),
            reverse=True,
        )
        assert all(item.kind == "agent_event" for item in first_events.activities)

        with pytest.raises(grpc.RpcError) as missing_error:
            stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id="missing-run"))
        assert missing_error.value.code() is grpc.StatusCode.NOT_FOUND

        with pytest.raises(grpc.RpcError) as token_error:
            stub.ListRunActivity(
                pb.ListRunActivityRequestV2(
                    run_id=run.run_id,
                    continuation=pb.ContinuationRefV2(
                        scope_ref=pb.ScopeReferenceV2(reference=operator.operator_instance_id),
                        continuation_id="not-a-token",
                    ),
                )
            )
        assert token_error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    finally:
        if channel is not None:
            channel.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_grpc_lazily_hydrates_paged_details_and_chunked_trace(
    monkeypatch,
):
    monkeypatch.setattr(
        "runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE",
        2,
    )
    operator = Operator([], watch=False, schedule=False)
    server = None
    provider = None
    try:
        run = _seed_hydration_run(operator, "run-hydration")
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        delegate = provider._stub

        class RecordingStub:
            def __init__(self):
                self.log_cursors = []
                self.event_cursors = []
                self.trace_chunks = 0
                self.trace_requests = []

            def __getattr__(self, name):
                return getattr(delegate, name)

            def ListRunActivity(self, request, **kwargs):  # noqa: N802
                if request.node_id:
                    self.event_cursors.append(request.continuation.continuation_id)
                else:
                    self.log_cursors.append(request.continuation.continuation_id)
                return delegate.ListRunActivity(request, **kwargs)

            def ReadActivityDetail(self, request, **kwargs):  # noqa: N802
                self.trace_requests.append(request)
                for chunk in delegate.ReadActivityDetail(request, **kwargs):
                    if request.detail_ref.object_uri.startswith("local://trace/"):
                        self.trace_chunks += 1
                    yield chunk

        recording = RecordingStub()
        provider._stub = recording
        hydrated = provider.get_run(run.run_id)

        assert hydrated is not None
        assert hydrated.details_hydrated
        assert len(hydrated.logs) == 6
        envelope = json.loads(hydrated.nodes["agent-1"].agent_trace_json)
        assert [event["sequence"] for event in envelope["events"]] == list(range(1, 6))
        assert envelope["trace"] is None
        assert len(recording.log_cursors) == 3
        assert len(recording.event_cursors) == 3

        with_trace = provider.hydrate_trace(run.run_id, "agent-1")
        assert with_trace is not None
        assert with_trace.operator_instance_id == operator.operator_instance_id
        assert with_trace.run_id == run.run_id
        assert with_trace.node_id == "agent-1"
        assert with_trace.trace_body["payload"].endswith("x" * 17)
        assert recording.trace_chunks == 3
        assert [
            request.detail_ref.scope_ref.reference
            for request in recording.trace_requests
            if request.detail_ref.object_uri.startswith("local://trace/")
        ] == [operator.operator_instance_id]
        assert provider.hydrate_trace(run.run_id, "agent-1") is not None
        assert recording.trace_chunks == 3

        page = operator.list_run_summaries(page_size=1)
        snapshot = operator.get_run_snapshot(
            run.run_id,
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
        )
        provider._install_structural_baseline(
            page.operator_instance_id,
            _event_ulid(page.as_of_sequence),
            {run.run_id: _run_from_snapshot(snapshot)},
        )
        assert not provider._runs_by_id[run.run_id].details_hydrated

        rehydrated = provider.get_run(run.run_id)
        assert rehydrated is not None
        assert len(rehydrated.logs) == 6
        envelope = json.loads(rehydrated.nodes["agent-1"].agent_trace_json)
        assert len(envelope["events"]) == 5
        assert envelope["trace"] is None
    finally:
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=1)
        operator.close()


def test_grpc_discards_hydration_after_epoch_reset(monkeypatch):
    monkeypatch.setattr(
        "runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE",
        2,
    )
    operator = Operator([], watch=False, schedule=False)
    server = None
    provider = None
    release = threading.Event()
    page_read = threading.Event()
    results = []
    errors = []
    try:
        run = _seed_hydration_run(operator, "run-stale-hydration")
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        delegate = provider._stub

        class BlockingStub:
            def __getattr__(self, name):
                return getattr(delegate, name)

            def ListRunActivity(self, request, **kwargs):  # noqa: N802
                response = delegate.ListRunActivity(request, **kwargs)
                if not page_read.is_set():
                    page_read.set()
                    if not release.wait(2):
                        raise RuntimeError("hydration barrier timed out")
                return response

        provider._stub = BlockingStub()

        def read_run():
            try:
                results.append(provider.get_run(run.run_id))
            except Exception as error:
                errors.append(error)

        reader = threading.Thread(target=read_run)
        reader.start()
        assert page_read.wait(2)
        provider._install_structural_baseline("replacement-epoch", _event_ulid(0), {})
        release.set()
        reader.join(2)
        assert not reader.is_alive()
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], _DetailHydrationRaceError)
        assert provider._cursor.operator_instance_id == "replacement-epoch"
        assert provider._runs_by_id == {}
    finally:
        release.set()
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=1)
        operator.close()


@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNAVAILABLE,
    ],
)
def test_detail_hydration_preserves_non_not_found_status(status):
    provider = GrpcStateProvider("localhost:1")

    class StatusError(grpc.RpcError):
        def code(self):
            return status

        def details(self):
            return "detail denied"

    class FailingDetailStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            return pb.RunSummaryPageV2(
                cursor=_cursor(2, stream="run-summaries"),
                scope_ref=_scope(),
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            return pb.RunSnapshotV2(
                cursor=_cursor(2, stream="run:run-1"),
                scope_ref=_scope(),
                summary=pb.RunSummaryV2(
                    run_id="run-1",
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=2,
                ),
                latest_log_sequence=1,
                log_continuation=pb.ContinuationRefV2(
                    scope_ref=_scope(),
                    continuation_id="logs-token",
                    cursor=_cursor(2, stream="activity:run-1:logs"),
                ),
            )

        def ListRunActivity(self, request, **kwargs):  # noqa: N802
            raise StatusError()

    provider._stub = FailingDetailStub()
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.get_run("run-1")
        assert error.value.status is status
        assert error.value.details == "detail denied"
    finally:
        provider.close()


def test_get_run_retries_from_fresh_cursor_after_hydration_restart():
    provider = GrpcStateProvider("localhost:1")

    class RestartedDuringHydration(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.FAILED_PRECONDITION

        def details(self):
            return "operator restarted during detail pagination"

    class RestartingHydrationStub:
        def __init__(self):
            self.summary_calls = 0
            self.snapshot_epochs = []
            self.log_calls = 0

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.summary_calls += 1
            return pb.RunSummaryPageV2(
                cursor=_cursor(self.summary_calls, stream="run-summaries"),
                scope_ref=_scope(f"operator-{self.summary_calls}"),
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.snapshot_epochs.append(f"operator-{self.summary_calls}")
            first_attempt = len(self.snapshot_epochs) == 1
            return pb.RunSnapshotV2(
                cursor=_cursor(self.summary_calls, stream=f"run:{request.run_id}"),
                scope_ref=_scope(f"operator-{self.summary_calls}"),
                summary=pb.RunSummaryV2(
                    run_id=request.run_id,
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=self.summary_calls,
                ),
                latest_log_sequence=1 if first_attempt else 0,
                log_continuation=(
                    pb.ContinuationRefV2(
                        scope_ref=_scope(f"operator-{self.summary_calls}"),
                        continuation_id="logs-token",
                        cursor=_cursor(
                            self.summary_calls,
                            stream="activity:run-1:logs",
                        ),
                    )
                    if first_attempt
                    else pb.ContinuationRefV2()
                ),
            )

        def ListRunActivity(self, request, **kwargs):  # noqa: N802
            self.log_calls += 1
            raise RestartedDuringHydration()

    stub = RestartingHydrationStub()
    provider._stub = stub
    try:
        run = provider.get_run("run-1")
    finally:
        provider.close()

    assert run is not None
    assert run.operator_instance_id == "operator-2"
    assert stub.summary_calls == 2
    assert stub.snapshot_epochs == ["operator-1", "operator-2"]
    assert stub.log_calls == 1


def test_get_run_rethrows_exact_hydration_restart_error_after_bounded_retries():
    provider = GrpcStateProvider("localhost:1")
    failure = OperatorCallError(
        grpc.StatusCode.FAILED_PRECONDITION,
        "operator restarted during hydration",
    )
    summary_calls = 0
    snapshot_cursors = []

    class SnapshotStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            nonlocal summary_calls
            summary_calls += 1
            return pb.RunSummaryPageV2(
                cursor=_cursor(summary_calls, stream="run-summaries"),
                scope_ref=_scope(f"operator-{summary_calls}"),
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            snapshot_cursors.append(f"operator-{summary_calls}")
            return pb.RunSnapshotV2(
                cursor=_cursor(summary_calls, stream=f"run:{request.run_id}"),
                scope_ref=_scope(f"operator-{summary_calls}"),
                summary=pb.RunSummaryV2(
                    run_id=request.run_id,
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=summary_calls,
                ),
            )

    def fail_hydration(_snapshot):
        raise failure

    provider._stub = SnapshotStub()
    provider._hydrate_run_snapshot = fail_hydration
    try:
        with pytest.raises(OperatorCallError) as error:
            provider.get_run("run-1")
    finally:
        provider.close()

    assert error.value is failure
    assert error.value.status is grpc.StatusCode.FAILED_PRECONDITION
    assert error.value.details == "operator restarted during hydration"
    assert summary_calls == 3
    assert snapshot_cursors == ["operator-1", "operator-2", "operator-3"]


def test_unrelated_updates_do_not_invalidate_detail_page_tokens(monkeypatch):
    monkeypatch.setattr(
        "runtime.operator.client.DETAIL_HYDRATION_PAGE_SIZE",
        2,
    )
    operator = Operator([], watch=False, schedule=False)
    server = None
    provider = None
    try:
        target = _seed_hydration_run(operator, "run-target")
        unrelated = RunState(run_id="run-unrelated", flow_name="flow")
        with operator._lock:
            operator._runs[unrelated.run_id] = unrelated
        operator._notify_run(unrelated)
        port = _unused_port()
        server = serve(operator, port=port, block=False)
        provider = GrpcStateProvider(f"localhost:{port}")
        delegate = provider._stub
        page_sequences = []
        updated = False

        class UpdatingStub:
            def __getattr__(self, name):
                return getattr(delegate, name)

            def ListRunActivity(self, request, **kwargs):  # noqa: N802
                nonlocal updated
                response = delegate.ListRunActivity(request, **kwargs)
                page_sequences.append(response.cursor.event_ulid)
                if not updated:
                    updated = True
                    unrelated.status = RunStatus.RUNNING
                    operator._notify_run(unrelated)
                return response

        provider._stub = UpdatingStub()
        hydrated = provider.get_run(target.run_id)
        assert hydrated is not None
        assert len(hydrated.logs) == 6
        assert len(json.loads(hydrated.nodes["agent-1"].agent_trace_json)["events"]) == 5
        assert len(page_sequences) > 1
        assert len(set(page_sequences)) == 1
    finally:
        if provider is not None:
            provider.close()
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()
