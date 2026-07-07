from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

import avalanche as ava


class ExampleInput(ava.BaseInput):
    value: int
    document: ava.File
    remote_document: ava.S3File


class ExampleContext(ava.RunContext):
    request_id: str


def test_workflow_run_validates_input_and_injects_context_without_consuming_data_args():
    @ava.source
    def load(payload: ExampleInput, ctx: ava.RunContext):
        assert ctx.workflow_name == "example_workflow"
        assert ctx.node_id == "load_1"
        assert payload.document.read_bytes() == b"hello"
        assert payload.document.open().read() == b"hello"
        assert isinstance(payload.document.open(), BytesIO)
        assert payload.remote_document.uri == "s3://bucket/key.txt"
        return payload.value

    @ava.step
    def add_context(ctx: ExampleContext, value):
        assert ctx.request_id == "req_123"
        assert ctx.node_id == "add_context_1"
        return value + 1

    @ava.workflow(input=ExampleInput, context=ExampleContext)
    def example_workflow():
        return load() >> add_context()

    result = example_workflow().run(
        executor=ava.LocalExecutor(),
        input={
            "value": 41,
            "document": {"name": "doc.txt", "content": b"hello"},
            "remote_document": {"uri": "s3://bucket/key.txt"},
        },
        context={"request_id": "req_123"},
    )

    assert result == 42


def test_workflow_run_injects_node_slug_into_run_context():
    @ava.source(slug="load-docs")
    def load(ctx: ava.RunContext):
        return ctx.node_id, ctx.node_name, ctx.node_slug

    @ava.workflow
    def slug_workflow():
        return load()

    result = slug_workflow().run(executor=ava.LocalExecutor())

    assert result == ("load_1", "load", "load-docs")


def test_workflow_run_run_id_overrides_mapping_context_runtime_fields():
    @ava.source
    def load(ctx: ExampleContext):
        return ctx.run_id, ctx.workflow_name, ctx.executor_type

    @ava.workflow(context=ExampleContext)
    def context_workflow():
        return load()

    result = context_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="external_run",
        context={
            "request_id": "req_123",
            "run_id": "spoofed_user_id",
            "workflow_name": "spoofed_workflow",
            "executor_type": "spoofed_executor",
        },
    )

    assert result == ("external_run", "context_workflow", "local")


def test_workflow_rejects_invalid_input_and_context_types_at_build_time():
    @ava.source
    def load():
        return "ok"

    @ava.workflow(input=int)
    def bad_input_workflow():
        return load()

    with pytest.raises(TypeError, match="input type"):
        bad_input_workflow()

    @ava.workflow(context=int)
    def bad_context_workflow():
        return load()

    with pytest.raises(TypeError, match="context type"):
        bad_context_workflow()


def test_workflow_run_rejects_unknown_input_and_context_fields():
    @ava.source
    def load(payload: ExampleInput, ctx: ExampleContext):
        return payload.value, ctx.request_id

    @ava.workflow(input=ExampleInput, context=ExampleContext)
    def strict_workflow():
        return load()

    valid_input = {
        "value": 41,
        "document": {"name": "doc.txt", "content": b"hello"},
        "remote_document": {"uri": "s3://bucket/key.txt"},
    }

    with pytest.raises(ValueError, match="Extra inputs"):
        strict_workflow().run(
            executor=ava.LocalExecutor(),
            input={**valid_input, "typo": "dropped"},
            context={"request_id": "req_123"},
        )

    with pytest.raises(ValueError, match="Extra inputs"):
        strict_workflow().run(
            executor=ava.LocalExecutor(),
            input=valid_input,
            context={"request_id": "req_123", "typo": "dropped"},
        )


def test_inline_file_payloads_are_bounded_and_hash_checked(tmp_path):
    content = b"small file"
    digest = hashlib.sha256(content).hexdigest()

    file = ava.File(content=content)
    assert file.sha256 == digest
    assert ava.File(content=content, sha256=digest.upper()).sha256 == digest

    with pytest.raises(ValueError, match="sha256"):
        ava.File(content=content, sha256="0" * 64)

    with pytest.raises(ValueError, match="S3File"):
        ava.File(content=b"x" * (ava.MAX_INLINE_FILE_BYTES + 1))

    large_path = tmp_path / "large.bin"
    large_path.write_bytes(b"x" * (ava.MAX_INLINE_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="S3File"):
        ava.File.from_path(large_path)


def test_workflow_run_rejects_invalid_s3_file_reference():
    class BadInput(ava.BaseInput):
        remote_document: ava.S3File

    @ava.source
    def load(payload: BadInput):
        return payload.remote_document.uri

    @ava.workflow(input=BadInput)
    def bad_workflow():
        return load()

    with pytest.raises(ValueError, match="s3://"):
        bad_workflow().run(
            executor=ava.LocalExecutor(),
            input={"remote_document": {"uri": "https://example.com/file"}},
        )
