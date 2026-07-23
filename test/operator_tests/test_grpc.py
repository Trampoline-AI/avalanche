"""Tests for gRPC server + client roundtrip."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import grpc
import pytest

from avalanche.operator import Operator
from avalanche.operator.client import GrpcStateProvider
from avalanche.operator.convert import (
    run_state_from_proto,
    run_state_to_proto,
    workflow_info_from_proto,
    workflow_info_to_proto,
)
from avalanche.operator.models import RunState, RunStatus, WorkflowInfo
from avalanche.operator.server import serve
from avalanche.runtime import File
from runtime.operator.proto import operator_pb2 as pb

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")
TEST_PORT = 17433  # Use non-default port to avoid conflicts


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


def test_start_run_wire_preserves_surviving_field_numbers():
    request = pb.StartRunRequest(
        flow_name="input_workflow",
        run_id="run_01KCVST2FP4QC5NKZNN5NS0Z2W",
        workflow_selector="flows/input.py::input_workflow",
    )

    assert request.run_id == "run_01KCVST2FP4QC5NKZNN5NS0Z2W"
    assert request.workflow_selector == "flows/input.py::input_workflow"
    assert pb.StartRunRequest.FLOW_NAME_FIELD_NUMBER == 1
    assert pb.StartRunRequest.INPUT_JSON_FIELD_NUMBER == 2
    assert pb.StartRunRequest.CONTEXT_JSON_FIELD_NUMBER == 3
    assert pb.StartRunRequest.INPUT_FILES_FIELD_NUMBER == 4
    assert pb.StartRunRequest.RUN_ID_FIELD_NUMBER == 6
    assert pb.StartRunRequest.WORKFLOW_SELECTOR_FIELD_NUMBER == 7
    assert set(pb.StartRunRequest.DESCRIPTOR.fields_by_number) == {1, 2, 3, 4, 6, 7}
    assert "S3FileReference" not in pb.DESCRIPTOR.message_types_by_name

    proto_source = Path(pb.__file__).with_name("operator.proto").read_text()
    assert "input_s3_files" not in proto_source
    assert "S3FileReference" not in proto_source


@pytest.fixture(scope="module")
def grpc_server():
    """Start a gRPC server with a real Operator for the test module."""
    op = Operator(
        workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
        schedule=False,
        watch=False,
    )
    server = serve(op, port=TEST_PORT, block=False)
    time.sleep(0.2)  # Let server bind
    yield op, server
    server.stop(grace=1)
    op.close()


@pytest.fixture
def client(grpc_server):
    """Create a GrpcStateProvider client connected to the test server."""
    provider = GrpcStateProvider(f"localhost:{TEST_PORT}")
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

    def test_start_run_honors_client_run_id(self, client):
        requested_run_id = "run_client_owned"

        assert client.start_run("simple_workflow", run_id=requested_run_id) == requested_run_id

    def test_start_run_duplicate_custom_id_maps_to_already_exists(self, client):
        requested_run_id = "run_grpc_duplicate"
        assert client.start_run("simple_workflow", run_id=requested_run_id) == requested_run_id

        assert client.start_run("simple_workflow", run_id=requested_run_id) == ""
        assert "ALREADY_EXISTS" in client.last_error

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
                pb.StartRunRequest(
                    flow_name="lineage_context_workflow",
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
            messages = [entry.message for entry in run.logs]
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
        start_request = pb.StartRunRequest(
            flow_name="input_workflow",
            run_id="run_bad_inline_checksum",
            input_files=[
                pb.FileAttachment(
                    field_name="document",
                    content=b"contents",
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
        messages = [entry.message for entry in run.logs]
        assert any("message=from-grpc" in message for message in messages)
        assert any("request_id=req_grpc" in message for message in messages)
        assert any(f"run_id={run_id}" in message for message in messages)
        assert not any("run_id=spoofed_user_id" in message for message in messages)
        assert any("file=grpc-bytes" in message for message in messages)

    def test_start_run_rejects_duplicate_top_level_input_fields(self, client):
        run_id = client.start_run(
            "input_workflow",
            input={"message": "from-grpc", "document": "json-value"},
            files={"document": File(name="note.txt", content=b"grpc-bytes")},
        )

        assert run_id == ""
        assert "INVALID_ARGUMENT" in client.last_error
        assert "Duplicate input field 'document'" in client.last_error

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

        # Cancellation is a request; the coordinator publishes the terminal
        # state after cooperative completion or the configured forced grace.
        run = client.get_run(run_id)
        assert run.status == RunStatus.RUNNING
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

    def test_stream_updates(self, client):
        """StreamUpdates should deliver live state changes."""
        updates = []

        def on_update(run):
            updates.append((run.run_id, run.status))

        client.on_run_update(on_update)
        time.sleep(0.2)  # Let stream connect

        run_id = client.start_run("simple_workflow")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(rid == run_id and s == RunStatus.SUCCESS for rid, s in updates):
                break
            time.sleep(0.05)

        # Should have received at least one update with the run completing
        statuses = [s for rid, s in updates if rid == run_id]
        assert len(statuses) > 0, "No stream updates received"
        assert RunStatus.SUCCESS in statuses

    def test_reconnect_replays_terminal_update_missed_while_disconnected(self, grpc_server):
        operator, _server = grpc_server
        run = RunState(run_id="run_reconnect", flow_name="flow")
        with operator._lock:
            operator._runs[run.run_id] = run
            cursor = operator._sequence
        provider = GrpcStateProvider(f"localhost:{TEST_PORT}")
        first_stream = None
        replay_stream = None
        try:
            operator._notify_run(run)
            first_stream = provider._stub.StreamUpdates(pb.StreamRequest(since_sequence=cursor))
            first = next(first_stream)
            assert first.run.run_id == run.run_id
            first_stream.cancel()

            run.status = RunStatus.SUCCESS
            operator._notify_run(run)

            replay_stream = provider._stub.StreamUpdates(
                pb.StreamRequest(since_sequence=first.sequence)
            )
            replay = next(replay_stream)
            assert replay.sequence > first.sequence
            assert replay.run.run_id == run.run_id
            assert replay.run.status == RunStatus.SUCCESS.value
        finally:
            if first_stream is not None:
                first_stream.cancel()
            if replay_stream is not None:
                replay_stream.cancel()
            provider.close()
            with operator._lock:
                operator._runs.pop(run.run_id, None)

    def test_legacy_flow_name_request_still_starts(self, client):
        response = client._stub.StartRun(pb.StartRunRequest(flow_name="simple_workflow"))
        assert response.run_id.startswith("run_")


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

    message = workflow_info_to_proto(info)
    assert message.file_path == "reports/daily.py"
    assert message.relative_file == "reports/daily.py"
    assert not os.path.isabs(message.file_path)
    restored = workflow_info_from_proto(message)
    assert restored.workflow_id == info.workflow_id
    assert restored.display_name == info.display_name
    assert restored.root_alias == info.root_alias
    assert restored.relative_file == info.relative_file
    assert restored.builder_symbol == info.builder_symbol
    assert restored.file_path == "reports/daily.py"

    run = RunState(
        run_id="run_1",
        flow_name="Shared report",
        workflow_id=info.workflow_id,
        workflow_display_name=info.display_name,
    )
    assert run_state_from_proto(run_state_to_proto(run)) == run


def test_old_proto_fields_receive_identity_fallbacks():
    info = workflow_info_from_proto(pb.FlowInfoMsg(name="legacy", file_path="legacy.py"))
    assert info.workflow_id == "legacy"
    assert info.display_name == "legacy"
    assert info.relative_file == "legacy.py"

    run = run_state_from_proto(
        pb.RunStateMsg(run_id="run_old", flow_name="legacy", status="pending")
    )
    assert run.workflow_id == "legacy"
    assert run.workflow_display_name == "legacy"


def test_client_accepts_lower_first_sequence_after_operator_restart():
    provider = GrpcStateProvider("localhost:1")
    received = []

    class RestartedStub:
        def StreamUpdates(self, request, *, metadata):  # noqa: N802
            assert request.since_sequence == 99
            assert metadata is None
            yield pb.RunUpdate(
                sequence=2,
                run=pb.RunStateMsg(run_id="run_recovered", flow_name="flow", status="success"),
            )
            yield pb.RunUpdate(
                sequence=2,
                run=pb.RunStateMsg(run_id="duplicate", flow_name="flow", status="success"),
            )
            yield pb.RunUpdate(
                sequence=1,
                run=pb.RunStateMsg(run_id="stale", flow_name="flow", status="pending"),
            )
            yield pb.RunUpdate(
                sequence=3,
                run=pb.RunStateMsg(run_id="run_live", flow_name="flow", status="running"),
            )
            provider._stream_stop.set()

    provider._stub = RestartedStub()
    provider._last_seq = 99
    provider._run_callbacks.append(lambda run: received.append(run.run_id))
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert received == ["run_recovered", "run_live"]
    assert provider._last_seq == 3


def test_client_skips_equal_first_sequence_without_epoch_reset():
    provider = GrpcStateProvider("localhost:1")
    received = []

    class DuplicateFirstStub:
        def StreamUpdates(self, request, *, metadata):  # noqa: N802
            assert request.since_sequence == 99
            assert metadata is None
            yield pb.RunUpdate(
                sequence=99,
                run=pb.RunStateMsg(run_id="duplicate", flow_name="flow", status="success"),
            )
            yield pb.RunUpdate(
                sequence=100,
                run=pb.RunStateMsg(run_id="run_live", flow_name="flow", status="running"),
            )
            provider._stream_stop.set()

    provider._stub = DuplicateFirstStub()
    provider._last_seq = 99
    provider._run_callbacks.append(lambda run: received.append(run.run_id))
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert received == ["run_live"]
    assert provider._last_seq == 100


def test_concurrent_run_update_registrations_start_one_stream_thread():
    provider = GrpcStateProvider("localhost:1")
    registration_count = 16
    barrier = threading.Barrier(registration_count + 1)
    stream_entered = threading.Event()
    release_stream = threading.Event()

    class BlockingStub:
        def __init__(self):
            self.calls = 0
            self.thread_ids = set()

        def StreamUpdates(self, request, *, metadata):  # noqa: N802
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

        def StreamUpdates(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            if close_returned.is_set():
                self.post_close_calls += 1
            raise Unavailable()

    stub = FailingStub()
    provider._stub = stub

    def register():
        barrier.wait()
        provider.on_run_update(lambda _run: None)

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

        def StreamUpdates(self, request, *, metadata):  # noqa: N802
            self.calls += 1
            called.set()
            raise Unavailable()

    stub = FailingStub()
    provider._stub = stub
    provider.connected = True
    provider.on_run_update(lambda _run: None)
    assert called.wait(timeout=1.0)

    provider.close()
    provider.close()
    calls_after_close = stub.calls
    provider.on_run_update(lambda _run: None)

    assert provider._stream_thread is not None
    assert not provider._stream_thread.is_alive()
    assert stub.calls == calls_after_close
    assert provider.connected is False


def test_canonical_client_requests_include_cached_legacy_name():
    canonical_id = "root/reports/daily.py::build_report"

    class CapturingStub:
        def __init__(self):
            self.start_request = None
            self.list_request = None

        def ListFlows(self, request, **kwargs):  # noqa: N802
            return pb.FlowList(
                flows=[
                    pb.FlowInfoMsg(
                        name="Daily report",
                        display_name="Daily report",
                        workflow_id=canonical_id,
                        file_path="reports/daily.py",
                    )
                ]
            )

        def StartRun(self, request, **kwargs):  # noqa: N802
            self.start_request = request
            return pb.StartRunResponse(run_id="run_legacy")

        def ListRuns(self, request, **kwargs):  # noqa: N802
            self.list_request = request
            return pb.RunList()

    provider = GrpcStateProvider("localhost:1")
    stub = CapturingStub()
    provider._stub = stub
    try:
        workflows = provider.list_workflows()
        assert workflows[0].workflow_id == canonical_id

        assert provider.start_run(canonical_id) == "run_legacy"
        assert stub.start_request.workflow_selector == canonical_id
        assert stub.start_request.flow_name == "Daily report"

        assert provider.list_runs(canonical_id) == []
        assert stub.list_request.workflow_selector == canonical_id
        assert stub.list_request.flow_name == "Daily report"
    finally:
        provider.close()


def test_unary_client_calls_pass_finite_timeout():
    calls = []

    class CapturingStub:
        def _capture(self, name, timeout):
            calls.append((name, timeout))
            assert timeout is not None
            assert 0 < timeout < float("inf")

        def ListFlows(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("list", timeout)
            return pb.FlowList()

        def StartRun(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("start", timeout)
            return pb.StartRunResponse(run_id="run_1")

        def GetRun(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("get", timeout)
            return pb.RunStateMsg(run_id=request.run_id, flow_name="flow", status="pending")

        def ListRuns(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("runs", timeout)
            return pb.RunList()

        def CancelRun(self, request, *, timeout, **kwargs):  # noqa: N802
            self._capture("cancel", timeout)
            return pb.Empty()

    provider = GrpcStateProvider("localhost:1", unary_timeout=3.5)
    provider._stub = CapturingStub()
    try:
        provider.list_workflows()
        provider.start_run("flow")
        provider.get_run("run_1")
        provider.list_runs("flow")
        provider.cancel_run("run_1")
    finally:
        provider.close()

    assert calls == [
        ("list", 3.5),
        ("start", 3.5),
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

        assert provider.start_run("shared") == ""
        assert "INVALID_ARGUMENT" in provider.last_error
        assert ids[0] in provider.last_error
        assert ids[1] in provider.last_error
        assert provider.list_runs("shared") == []
        assert "INVALID_ARGUMENT" in provider.last_error

        (roots[0] / "flow.py").write_text("VALUE = 1\n")
        assert provider.start_run(ids[0]) == ""
        assert "FAILED_PRECONDITION" in provider.last_error
        assert "preparation failed" in provider.last_error.lower()
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

        def add_insecure_port(self, _address):
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
        "add_OperatorServiceServicer_to_server",
        lambda _servicer, _server: None,
    )

    assert server_module.serve(operator, port=0, block=True) is fake_server
    assert operator.closed
    assert fake_server.stop_calls
    assert dict(grpc_options) == {
        "grpc.max_send_message_length": -1,
        "grpc.max_receive_message_length": -1,
    }
