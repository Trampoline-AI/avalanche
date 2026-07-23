from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

import avalanche as ava


class ExampleInput(ava.BaseInput):
    value: int
    document: ava.File


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
        return payload.value

    @ava.step
    def add_context(ctx: ExampleContext, value):
        assert ctx.request_id == "req_123"
        assert ctx.node_id == "add_context_1"
        return value + 1

    @ava.workflow(input=ExampleInput, context=ExampleContext)
    def example_workflow():
        return load() >> add_context()

    result = (
        example_workflow()
        .run(
            executor=ava.LocalExecutor(),
            input={
                "value": 41,
                "document": {"name": "doc.txt", "content": b"hello"},
            },
            context={"request_id": "req_123"},
        )
        .result()
    )

    assert result == 42


def test_workflow_run_injects_node_slug_into_run_context():
    @ava.source(slug="load-docs")
    def load(ctx: ava.RunContext):
        return ctx.node_id, ctx.node_name, ctx.node_slug

    @ava.workflow
    def slug_workflow():
        return load()

    result = slug_workflow().run(executor=ava.LocalExecutor()).result()

    assert result == ("load_1", "load", "load-docs")


def test_workflow_run_run_id_overrides_mapping_context_runtime_fields():
    @ava.source
    def load(ctx: ExampleContext):
        return ctx.run_id, ctx.workflow_name, ctx.executor_type

    @ava.workflow(context=ExampleContext)
    def context_workflow():
        return load()

    result = (
        context_workflow()
        .run(
            executor=ava.LocalExecutor(),
            run_id="external_run",
            context={
                "request_id": "req_123",
                "run_id": "spoofed_user_id",
                "workflow_name": "spoofed_workflow",
                "executor_type": "spoofed_executor",
            },
        )
        .result()
    )

    assert result == ("external_run", "context_workflow", "local")


def test_workflow_run_runtime_fields_override_mapping_context_lineage_and_node_fields():
    @ava.source(slug="load-docs")
    def load(ctx: ExampleContext):
        return {
            "run_id": ctx.run_id,
            "workflow_name": ctx.workflow_name,
            "executor_type": ctx.executor_type,
            "rerun": ctx.rerun,
            "node_id": ctx.node_id,
            "node_name": ctx.node_name,
            "node_slug": ctx.node_slug,
            "lineage_vector": ctx.lineage_vector,
            "request_id": ctx.request_id,
        }

    @ava.workflow(context=ExampleContext)
    def context_workflow():
        return load()

    result = (
        context_workflow()
        .run(
            executor=ava.LocalExecutor(),
            run_id="run_real",
            context={
                "request_id": "req_123",
                "run_id": "run_fake",
                "workflow_name": "fake_workflow",
                "executor_type": "fake_executor",
                "rerun": {"run_id": "fake_parent", "start": ["fake-node"]},
                "node_id": "fake_node_1",
                "node_name": "fake_node",
                "node_slug": "fake-node",
                "lineage_vector": {"upstream": "run_fake"},
            },
        )
        .result()
    )

    assert result == {
        "run_id": "run_real",
        "workflow_name": "context_workflow",
        "executor_type": "local",
        "rerun": None,
        "node_id": "load_1",
        "node_name": "load",
        "node_slug": "load-docs",
        "lineage_vector": {},
        "request_id": "req_123",
    }


def test_workflow_run_sanitizes_seed_lineage_but_preserves_real_parent_lineage():
    @ava.source(slug="upstream")
    def load(ctx: ava.RunContext):
        return {"root_lineage": dict(ctx.lineage_vector)}

    @ava.step(slug="downstream")
    def consume(payload, ctx: ava.RunContext):
        return {
            "payload": payload,
            "downstream_lineage": dict(ctx.lineage_vector),
        }

    @ava.workflow
    def lineage_workflow():
        return load() >> consume()

    result = (
        lineage_workflow()
        .run(
            executor=ava.LocalExecutor(),
            run_id="run_real",
            context={"lineage_vector": {"upstream": "run_fake"}},
        )
        .result()
    )

    assert result == {
        "payload": {"root_lineage": {}},
        "downstream_lineage": {"upstream": "run_real"},
    }


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
    }

    with pytest.raises(ValueError, match="Extra inputs"):
        strict_workflow().run(
            executor=ava.LocalExecutor(),
            input={**valid_input, "typo": "dropped"},
            context={"request_id": "req_123"},
        ).result()

    with pytest.raises(ValueError, match="Extra inputs"):
        strict_workflow().run(
            executor=ava.LocalExecutor(),
            input=valid_input,
            context={"request_id": "req_123", "typo": "dropped"},
        ).result()


def test_file_payloads_are_unbounded_and_hash_checked(tmp_path):
    content = b"x" * (4 * 1024 * 1024 + 1)
    digest = hashlib.sha256(content).hexdigest()

    file = ava.File(content=content)
    assert file.sha256 == digest
    assert ava.File(content=content, sha256=digest.upper()).sha256 == digest

    with pytest.raises(ValueError, match="sha256"):
        ava.File(content=content, sha256="0" * 64)

    large_path = tmp_path / "large.bin"
    large_path.write_bytes(content)
    from_path = ava.File.from_path(large_path)
    assert from_path.name == "large.bin"
    assert from_path.content == content
    assert from_path.sha256 == digest


def test_file_size_limit_constants_are_not_public():
    import avalanche.runtime as ava_runtime

    for module in (ava, ava_runtime):
        assert not hasattr(module, "MAX_INLINE_FILE_BYTES")
        assert not hasattr(module, "MAX_INLINE_REQUEST_BYTES")


def test_s3_file_contract_is_not_public():
    import avalanche.runtime as ava_runtime

    for module in (ava, ava_runtime):
        assert not hasattr(module, "S3File")
        assert "S3File" not in module.__all__
