"""Tests for gRPC server + client roundtrip."""

import os
import time

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


@pytest.fixture(scope="module")
def grpc_server():
    """Start a gRPC server with a real Operator for the test module."""
    op = Operator(
        workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
        schedule=False,
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

    def test_start_run_passes_json_context_and_file_attachments(self, client):
        run_id = client.start_run(
            "input_workflow",
            input={"message": "from-grpc"},
            context={"request_id": "req_grpc", "execution_id": "spoofed_user_id"},
            files={"document": File(name="note.txt", content=b"grpc-bytes")},
            s3_files={"document_ref": S3File(uri="s3://bucket/grpc.txt")},
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
        assert any(f"execution_id={run_id}" in message for message in messages)
        assert not any("execution_id=spoofed_user_id" in message for message in messages)
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
