from __future__ import annotations

import hashlib
import socket
import time

import pytest

import avalanche as ava
from avalanche.operator import Operator
from avalanche.operator.client import GrpcStateProvider
from avalanche.operator.models import RunStatus
from avalanche.operator.server import serve


class ArtifactInput(ava.BaseInput):
    document: ava.ArtifactRef
    remote_document: ava.ArtifactRef


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def _wait_for_terminal_run(client: GrpcStateProvider, run_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.05)
    return client.get_run(run_id)


def test_workflow_stages_inputs_registers_remote_objects_and_publishes_outputs(tmp_path):
    store = ava.LocalArtifactStore(tmp_path / "artifacts")
    scratch = tmp_path / "executor-scratch" / "proposal.txt"

    @ava.source(slug="build-proposal")
    def build(payload: ArtifactInput, ctx: ava.RunContext):
        assert isinstance(payload.document, ava.ArtifactRef)
        assert payload.document.read_bytes() == b"submitted document"
        assert payload.remote_document.uri == "s3://existing-bucket/reference.pdf"
        scratch.parent.mkdir()
        scratch.write_bytes(b"generated proposal")
        published = ctx.publish_artifact(scratch, role="proposal")
        return published, ctx.artifact_manifest()

    @ava.workflow(input=ArtifactInput, artifact_store=store)
    def artifact_workflow():
        return build()

    run_id = "run-artifact-lineage"
    output, manifest_during_run = (
        artifact_workflow()
        .run(
            executor=ava.LocalExecutor(),
            run_id=run_id,
            input={
                "document": ava.File(
                    name="brief.txt",
                    content=b"submitted document",
                    content_type="text/plain",
                ),
                "remote_document": ava.S3File(
                    uri="s3://existing-bucket/reference.pdf",
                    size_bytes=421,
                    content_type="application/pdf",
                    sha256="a" * 64,
                ),
            },
        )
        .result()
    )

    scratch.unlink()
    manifest = artifact_workflow().artifact_manifest(run_id)

    assert manifest == manifest_during_run
    assert manifest.run_id == run_id
    assert [ref.name for ref in manifest.inputs] == ["brief.txt", "reference.pdf"]
    assert [ref.name for ref in manifest.outputs] == ["proposal.txt"]

    staged = manifest.inputs[0]
    assert staged.uri.startswith("file://")
    assert staged.checksum == hashlib.sha256(b"submitted document").hexdigest()
    assert staged.size == len(b"submitted document")
    assert staged.media_type == "text/plain"
    assert staged.kind == "input"
    assert staged.run_id == run_id
    assert staged.node_id is None
    assert staged.role == "document"
    assert staged.origin == "upload://brief.txt"
    assert staged.read_bytes() == b"submitted document"

    registered = manifest.inputs[1]
    assert registered.uri == "s3://existing-bucket/reference.pdf"
    assert registered.checksum == "a" * 64
    assert registered.size == 421
    assert registered.media_type == "application/pdf"
    assert registered.kind == "input"
    assert registered.run_id == run_id
    assert registered.node_id is None
    assert registered.role == "remote_document"
    assert registered.origin == registered.uri

    assert output == manifest.outputs[0]
    assert output.read_bytes() == b"generated proposal"
    assert output.checksum == hashlib.sha256(b"generated proposal").hexdigest()
    assert output.size == len(b"generated proposal")
    assert output.media_type == "text/plain"
    assert output.kind == "output"
    assert output.run_id == run_id
    assert output.node_id == "build_1"
    assert output.role == "proposal"
    assert output.origin == scratch.resolve().as_uri()


def test_publication_is_atomic_and_rejects_duplicate_run_kind_names(tmp_path):
    store = ava.LocalArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "result.txt"
    source.write_bytes(b"first")

    first = store.publish(
        source,
        run_id="run-duplicate",
        node_id="first_1",
        role="result",
    )
    source.write_bytes(b"second")

    with pytest.raises(ava.DuplicateArtifactError, match="already exists"):
        store.publish(
            source,
            run_id="run-duplicate",
            node_id="second_1",
            role="result",
        )

    assert first.read_bytes() == b"first"
    assert store.manifest("run-duplicate").artifacts == (first,)


def test_run_context_requires_configured_store_to_publish(tmp_path):
    context = ava.RunContext(
        run_id="run-no-store",
        workflow_name="missing_store",
        node_id="node_1",
    )
    source = tmp_path / "output.txt"
    source.write_text("output")

    with pytest.raises(RuntimeError, match="no artifact_store configured"):
        context.publish_artifact(source, role="result")


def test_operator_stages_cli_upload_before_nodes_and_keeps_manifest_after_teardown(tmp_path):
    artifact_root = tmp_path / "operator-artifacts"
    workflow_path = tmp_path / "artifact_workflow.py"
    workflow_path.write_text(
        f"""\
from pathlib import Path
import avalanche as ava

store = ava.LocalArtifactStore({str(artifact_root)!r})

class Input(ava.BaseInput):
    document: ava.ArtifactRef

@ava.source

def publish(payload: Input, ctx: ava.RunContext):
    assert payload.document.kind == "input"
    assert payload.document.run_id == ctx.run_id
    assert payload.document.read_bytes() == b"operator upload"
    generated = Path({str(tmp_path / "operator-scratch.txt")!r})
    generated.write_bytes(payload.document.read_bytes().upper())
    return ctx.publish_artifact(generated, role="normalized")

@ava.workflow(input=Input, artifact_store=store)
def operator_artifact_workflow():
    return publish()
"""
    )
    operator = Operator(workflow_paths=[str(workflow_path)], schedule=False, watch=False)
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    client = GrpcStateProvider(f"localhost:{port}")
    run_id = "run-operator-artifacts"
    try:
        assert (
            client.start_run(
                "operator_artifact_workflow",
                run_id=run_id,
                files={"document": ava.File(name="upload.txt", content=b"operator upload")},
            )
            == run_id
        )
        run = _wait_for_terminal_run(client, run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS
    finally:
        client.close()
        server.stop(grace=1)
        operator.close()

    manifest = ava.LocalArtifactStore(artifact_root).manifest(run_id)
    assert [ref.kind for ref in manifest.artifacts] == ["input", "output"]
    assert manifest.inputs[0].read_bytes() == b"operator upload"
    assert manifest.outputs[0].read_bytes() == b"OPERATOR UPLOAD"
    assert manifest.outputs[0].node_id == "publish_1"
