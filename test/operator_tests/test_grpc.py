"""Tests for gRPC server + client roundtrip."""

import json
import os
import socket
import time

import grpc
import pytest

import avalanche as ava
from avalanche.operator import Operator
from avalanche.operator.client import GrpcStateProvider
from avalanche.operator.models import RunStatus
from avalanche.operator.server import serve
from avalanche.runtime import MAX_INLINE_REQUEST_BYTES, File, S3File
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


def test_phase9_start_run_wire_carries_run_id_and_s3_sha256():
    request = pb.StartRunRequest(
        flow_name="input_workflow",
        run_id="run_01KCVST2FP4QC5NKZNN5NS0Z2W",
        input_s3_files=[
            pb.S3FileReference(
                field_name="document_ref",
                uri="s3://bucket/document",
                size_bytes=5,
                sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            )
        ],
    )

    assert request.run_id == "run_01KCVST2FP4QC5NKZNN5NS0Z2W"
    assert request.input_s3_files[0].sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


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
        client = GrpcStateProvider(f"localhost:{port}")
        try:
            run_id = "run_grpc_real"
            response = client._stub.StartRun(
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
            run = _wait_for_run_success(client, run_id)
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
            client.close()
            server.stop(grace=1)

    @pytest.mark.parametrize(
        "start_request",
        [
            pb.StartRunRequest(
                flow_name="input_workflow",
                run_id="run_bad_inline_checksum",
                input_files=[
                    pb.FileAttachment(
                        field_name="document",
                        content=b"contents",
                        sha256="0" * 64,
                    )
                ],
            ),
            pb.StartRunRequest(
                flow_name="input_workflow",
                run_id="run_bad_s3_checksum_shape",
                input_s3_files=[
                    pb.S3FileReference(
                        field_name="document_ref",
                        uri="s3://bucket/document",
                        sha256="not-a-digest",
                    )
                ],
            ),
            pb.StartRunRequest(
                flow_name="input_workflow",
                run_id="run_bad_s3_uri_shape",
                input_s3_files=[
                    pb.S3FileReference(
                        field_name="document_ref",
                        uri="https://bucket.example/document",
                    )
                ],
            ),
        ],
    )
    def test_start_run_rejects_bad_file_metadata_before_response(self, client, start_request):
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
            s3_files={
                "document_ref": S3File(
                    uri="s3://bucket/grpc.txt",
                    sha256="0" * 64,
                )
            },
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
        assert any("s3=s3://bucket/grpc.txt" in message for message in messages)

    def test_start_run_rejects_duplicate_top_level_input_fields(self, client):
        run_id = client.start_run(
            "input_workflow",
            input={"message": "from-grpc", "document": "json-value"},
            files={"document": File(name="note.txt", content=b"grpc-bytes")},
        )

        assert run_id == ""
        assert "INVALID_ARGUMENT" in client.last_error
        assert "Duplicate input field 'document'" in client.last_error

    def test_start_run_rejects_oversized_file_attachments(self, client):
        with pytest.raises(ValueError, match="S3File"):
            client.start_run(
                "input_workflow",
                files={"document": b"x" * (ava.MAX_INLINE_FILE_BYTES + 1)},
            )

    def test_start_run_rejects_aggregate_inline_file_bytes_before_rpc(self, client):
        chunk = b"x" * (MAX_INLINE_REQUEST_BYTES // 2 + 1)

        with pytest.raises(ValueError, match="maximum inline request size"):
            client.start_run(
                "simple_workflow",
                files={"first": chunk, "second": chunk},
            )

    def test_start_run_server_rejects_aggregate_inline_file_bytes(self, client):
        chunk = b"x" * (MAX_INLINE_REQUEST_BYTES // 2 + 1)

        with pytest.raises(Exception) as exc_info:
            client._stub.StartRun(
                pb.StartRunRequest(
                    flow_name="simple_workflow",
                    input_files=[
                        pb.FileAttachment(field_name="first", content=chunk),
                        pb.FileAttachment(field_name="second", content=chunk),
                    ],
                )
            )

        assert exc_info.value.code().name == "INVALID_ARGUMENT"
        assert "maximum inline request size" in exc_info.value.details()

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

        time.sleep(0.5)
        run = client.get_run(run_id)
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
            if any(s == RunStatus.SUCCESS for _, s in updates):
                break
            time.sleep(0.05)

        # Should have received at least one update with the run completing
        statuses = [s for rid, s in updates if rid == run_id]
        assert len(statuses) > 0, "No stream updates received"
        assert RunStatus.SUCCESS in statuses
