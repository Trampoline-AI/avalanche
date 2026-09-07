"""User context cannot replace scheduler-owned identity or producer lineage."""

import pytest

import avalanche as ava


class ExampleInput(ava.BaseInput):
    value: int
    document: ava.File


class ExampleContext(ava.RunContext):
    request_id: str


def test_validated_input_and_custom_context_do_not_consume_data_arguments():
    @ava.source
    def load(payload: ExampleInput):
        return payload.value, payload.document.open().read()

    @ava.step
    def consume(ctx: ExampleContext, value, content):
        return value + 1, content.decode(), ctx.request_id

    @ava.workflow(input=ExampleInput, context=ExampleContext)
    def flow():
        return load() >> consume()

    result = (
        flow()
        .run(
            executor=ava.LocalExecutor(),
            input={"value": 41, "document": {"name": "doc.txt", "content": b"hello"}},
            context={"request_id": "req-123"},
        )
        .result(timeout=5)
    )
    assert result == (42, "hello", "req-123")


def test_runtime_identity_overrides_user_context_but_preserves_real_parent_lineage():
    @ava.source(slug="upstream")
    def load(ctx: ExampleContext):
        assert ctx.lineage_vector == {}
        assert ctx.rerun is None
        return ctx.run_id, ctx.workflow_name, ctx.executor_type, ctx.node_slug

    @ava.step(slug="downstream")
    def consume(value, ctx: ExampleContext):
        return value, dict(ctx.lineage_vector), ctx.node_slug, ctx.request_id

    @ava.workflow(context=ExampleContext)
    def flow():
        return consume(load())

    result = (
        flow()
        .run(
            executor=ava.LocalExecutor(),
            run_id="real-run",
            context={
                "request_id": "req-123",
                "run_id": "fake-run",
                "workflow_name": "fake-workflow",
                "executor_type": "fake-executor",
                "rerun": {"run_id": "fake-parent", "start": ["fake-node"]},
                "node_id": "fake-node-1",
                "node_name": "fake-node",
                "node_slug": "fake-node",
                "lineage_vector": {"upstream": "fake-run"},
            },
        )
        .result(timeout=5)
    )
    assert result == (
        ("real-run", "flow", "local", "upstream"),
        {"upstream": "real-run"},
        "downstream",
        "req-123",
    )


@pytest.mark.parametrize("unknown_field_target", ["input", "context"])
def test_unknown_fields_fail_instead_of_silently_dropping_user_data(unknown_field_target):
    @ava.source
    def load(payload: ExampleInput, ctx: ExampleContext):
        raise AssertionError("invalid input must not reach user code")

    @ava.workflow(input=ExampleInput, context=ExampleContext)
    def flow():
        return load()

    arguments = {
        "input": {"value": 41, "document": {"content": b"hello"}},
        "context": {"request_id": "req-123"},
    }
    arguments[unknown_field_target]["typo"] = "must not disappear"
    with pytest.raises(ValueError, match="Extra inputs"):
        flow().run(executor=ava.LocalExecutor(), **arguments).result(timeout=5)
