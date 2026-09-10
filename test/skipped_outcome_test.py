"""Intentional absence survives real execution, storage, and value-edge boundaries."""

import json
from concurrent.futures import CancelledError
from pathlib import Path

import dataframely as dy
import polars as pl
import pytest
from pydantic import BaseModel, Field

import avalanche as ava
from runtime.operator.hooks import RunHooks
from runtime.operator.results import decode_workflow_result, encode_workflow_result


@pytest.fixture(params=["local", pytest.param("ray", marks=pytest.mark.ray)])
def executor(request):
    if request.param == "local":
        yield ava.LocalExecutor()
    else:
        import ray

        ray.init(
            num_cpus=2, include_dashboard=False, _node_ip_address="127.0.0.1",
            runtime_env={"env_vars": {"PYTHONPATH": str(Path(__file__).parent)}},
        )
        try:
            yield ava.RayExecutor()
        finally:
            ray.shutdown()


@pytest.fixture(params=["RunContext", "CustomContext"])
def context_type(request):
    if request.param == "RunContext":
        return ava.RunContext

    from fixtures.skipped_workflows import ApplicationContext

    return ApplicationContext


def test_executor_status_distinguishes_skip_from_successful_none(executor):
    def absent():
        return ava.skip("no payload", {"count": 0})

    def empty():
        return None

    _, absent_status = executor.submit_with_status(absent)
    _, empty_status = executor.submit_with_status(empty)
    assert executor.get([absent_status, empty_status]) == [
        ava.skip("no payload", {"count": 0}), None,
    ]
    # Single-return containers and explicitly returned equal slots are values,
    # not evidence that the author omitted the whole node.
    for value, count in (
        ([ava.skip("nested")], 1),
        ([ava.skip("nested"), ava.skip("nested")], 1),
        ((ava.skip("nested"), ava.skip("nested")), 1),
        (([ava.skip("nested")], [ava.skip("nested")]), 1),
        ((ava.skip("nested"), ava.skip("nested")), 2),
    ):
        payload, status = executor.submit_with_status(lambda: value, num_returns=count)
        assert executor.get([status]) == [None]
        actual = executor.get(list(payload)) if count > 1 else executor.get([payload])[0]
        assert actual == (list(value) if count > 1 else value)


def test_skip_satisfies_dependency_and_preserves_fan_in_value_positions(executor):
    skips, successes = {}, []

    @ava.source
    def absent():
        return ava.skip("no eligible records", {"count": 0, "partition": "today"})

    @ava.source
    def empty():
        return None

    @ava.step
    def join(left, right, context: ava.RunContext):
        assert isinstance(left, ava.Skipped)
        assert right is None
        return left.reason, right, context.lineage_vector

    @ava.step
    def dependency_only():
        return "dependency satisfied"

    @ava.workflow
    def flow():
        left, right = absent(), empty()
        joined = (left & right) >> join()
        tail = left >> dependency_only()
        return left, joined, tail

    handle = flow().run(
        executor=executor,
        run_id="skipped-fan-in",
        hooks=RunHooks(
            on_node_skipped=lambda node, outcome: skips.update({node: outcome}),
            on_node_success=successes.append,
        ),
    )
    absent_result, joined, tail = handle.result(timeout=60)
    assert absent_result == ava.skip("no eligible records", {"count": 0, "partition": "today"})
    assert joined == (
        "no eligible records", None, {"absent": "skipped-fan-in", "empty": "skipped-fan-in"},
    )
    assert tail == "dependency satisfied"
    assert list(skips.values()) == [absent_result]
    assert not set(skips).intersection(successes)
    assert len(successes) == 3
    assert decode_workflow_result(encode_workflow_result((absent_result, None))) == (
        absent_result, None,
    )


@pytest.mark.parametrize("unwrap", [False, True])
def test_multi_return_and_indexed_skips_are_non_values(executor, context_type, unwrap):
    skips, successes = {}, []
    @ava.source(num_returns=2)
    async def absent():
        return ava.skip("whole node omitted")

    @ava.step
    def combine(left, right):
        return isinstance(left, ava.Skipped) and left == right

    @ava.workflow(context=context_type)
    def flow():
        pair = absent()
        combined = (pair[0] & pair[1]) >> combine()
        return pair[1], combined

    hooks = RunHooks(
        on_node_skipped=lambda node, outcome: skips.update({node: outcome}),
        on_node_success=successes.append,
        unwrap_result=(lambda node, value: value) if unwrap else None,
    )
    assert flow().run(executor=executor, hooks=hooks).result(timeout=60) == (
        ava.skip("whole node omitted"), True,
    )
    assert skips == {"absent_1": ava.skip("whole node omitted")}
    assert successes == ["combine_1"]


def test_single_return_projection_preserves_skip_but_containers_are_values(
    executor, context_type,
):
    skips, successes = [], []

    @ava.source
    def absent():
        return ava.skip("no tuple today")

    @ava.source
    def container():
        return [ava.skip("retained nested outcome"), ava.skip("retained nested outcome")]

    @ava.step
    def inspect(value):
        return value.reason

    @ava.workflow(context=context_type)
    def flow():
        single = absent()
        return inspect(single[0]), container()

    assert flow().run(
        executor=executor,
        hooks=RunHooks(
            on_node_skipped=lambda node, outcome: skips.append(node),
            on_node_success=successes.append,
        ),
    ).result(timeout=60) == (
        "no tuple today",
        [ava.skip("retained nested outcome"), ava.skip("retained nested outcome")],
    )
    assert skips == ["absent_1"]
    assert set(successes) == {"container_1", "inspect_1"}


def test_failure_and_cancellation_do_not_become_authored_skips():
    skips, starts = [], []

    @ava.source
    def broken():
        raise RuntimeError("actual failure")

    @ava.step
    def downstream():
        starts.append("downstream")

    @ava.workflow
    def flow():
        broken() >> downstream()

    hooks = RunHooks(on_node_skipped=lambda *args: skips.append(args))
    with pytest.raises(RuntimeError, match="actual failure"):
        flow().run(executor=ava.LocalExecutor(), hooks=hooks).result()
    hooks.cancel_requested = lambda: True
    with pytest.raises(CancelledError):
        flow().run(executor=ava.LocalExecutor(), hooks=hooks).result()
    assert skips == starts == []


class Row(dy.Schema):
    id = dy.Int64(nullable=False)


@pytest.fixture(params=["iceberg", "lance"])
def namespace(request, tmp_path):
    if request.param == "iceberg":
        base, config, table = ava.IcebergNs, ava.IcebergNsConfig, ava.IcebergTable
        kwargs = {"catalog": "skip", "load_catalog_props": {
            "type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}",
        }}
    else:
        base, config, table = ava.LanceNamespace, ava.LanceNamespaceConfig, ava.LanceTable
        kwargs = {}

    class Store(base):
        ns_config = config(name="skip", base_location=str(tmp_path / "warehouse"))
        rows = table(schema=Row)

    ns = Store(**kwargs)
    ns.push()
    return ns


def test_persisted_skip_shadows_ancestor_rows_without_advancing_cursor(namespace, executor):
    table = namespace.rows

    @ava.source(slug="producer")
    def produce(context: ava.RunContext):
        if context.run_id == "original":
            return table.append(pl.DataFrame({"id": [1]}))
        return table.append(ava.skip("partition excluded", {"partition": "today"}))

    @ava.step(slug="consumer")
    def consume(rows=ava.Stream(table, mode="append_scan", key="skip_consumer")):
        return rows.height

    @ava.workflow
    def flow():
        return produce() >> consume()

    assert flow().run(executor=executor, run_id="original").result(timeout=60) == 1
    @ava.source(slug="other")
    def other():
        return table.append(pl.DataFrame({"id": [2]}))

    @ava.workflow
    def other_flow():
        other()

    other_flow().run(executor=executor, run_id="original").result(timeout=60)
    table.refresh()
    cursor = ava.ProgressStore(table, key="skip_consumer").get_cursor()
    assert flow().run(executor=executor, run_id="omitted", rerun=ava.Rerun(
        run_id="original", start=["producer"],
    )).result(timeout=60) == 0
    table.refresh()
    assert sorted(table.read()["id"].to_list()) == [1, 2]
    receipts = [
        json.loads(value) for key, value in table.properties.items()
        if key.startswith("avalanche.skip.")
    ]
    assert receipts == [{
        "run_id": "omitted", "node_slug": "producer", "reason": "partition excluded",
        "metadata": {"partition": "today"},
    }]
    # A lazy rerun must not resurrect original rows, including through sparse ancestry.
    for run_id, parent in (("replay", "omitted"), ("replay-again", "replay")):
        assert flow().run(executor=executor, run_id=run_id, rerun=ava.Rerun(
            run_id=parent, start=["consumer"], mode="lazy",
        )).result(timeout=60) == 0
    assert ava.ProgressStore(table, key="skip_consumer").get_cursor() == cursor
    # Empty producer versions shadow only their own slug, not sibling producers.
    with ava.consume_stream(
        table, rerun=ava.Rerun(run_id="replay-again", start=["consumer"])
    ) as rows:
        assert rows["id"].to_list() == [2]


def test_null_slug_replay_survives_named_skips_and_sparse_ancestry(namespace):
    from avalanche.runtime.context import run_with_context

    table = namespace.rows
    run_with_context(
        ava.RunContext(run_id="original", workflow_name="flow"),
        table.append,
        pl.DataFrame({"id": [1]}),
    )
    run_with_context(
        ava.RunContext(run_id="original", workflow_name="flow", node_slug="named"),
        table.append,
        pl.DataFrame({"id": [2]}),
    )
    with ava.consume_stream(
        table, rerun=ava.Rerun(run_id="original", start=["consumer"]),
    ) as rows:
        assert sorted(rows["id"].to_list()) == [1, 2]

    run_with_context(
        ava.RunContext(
            run_id="omitted", workflow_name="flow", node_slug="named",
            rerun=ava.Rerun(run_id="original", start=["named"]),
        ),
        table.append,
        ava.skip("named producer omitted"),
    )
    def replay():
        with ava.consume_stream(table) as rows:
            assert rows["id"].to_list() == [1]
            assert rows["_ava_node_slug"].to_list() == [None]

    # consume_stream records the sparse ancestry edge even without new rows.
    for run_id, parent in (("replay", "omitted"), ("again", "replay")):
        run_with_context(
            ava.RunContext(
                run_id=run_id, workflow_name="flow", node_slug="consumer",
                rerun=ava.Rerun(run_id=parent, start=["consumer"]),
            ),
            replay,
        )
    assert sorted(table.read()["id"].to_list()) == [1, 2]


def test_write_then_skip_keeps_physical_rows_but_replays_empty_schema(namespace):
    table = namespace.rows

    @ava.source(slug="producer")
    def producer():
        table.append(pl.DataFrame({"id": [1]}))
        return table.append(ava.skip("discard producer version"))

    @ava.step(slug="consumer")
    def consumer(rows=ava.Stream(table)):
        return rows.to_dicts(), rows.schema

    @ava.workflow
    def flow():
        return producer() >> consumer()

    live, _ = flow().run(executor=ava.LocalExecutor(), run_id="first").result()
    assert live == []
    replay, replay_schema = flow().run(
        executor=ava.LocalExecutor(),
        run_id="second",
        rerun=ava.Rerun(run_id="first", start=["consumer"], mode="lazy"),
    ).result()
    assert replay == []
    assert replay_schema == table.read().schema
    assert table.read()["id"].to_list() == [1]


def test_pydantic_results_preserve_nested_native_skip_and_public_metadata():
    class Output(BaseModel):
        outcome: ava.Skipped = Field(serialization_alias="omitted")
        history: list[ava.Skipped]

    outcome = ava.skip("intentional", {"count": 0, "nested": [None, True]})
    model = Output(outcome=outcome, history=[outcome])
    public = {"reason": outcome.reason, "metadata": outcome.metadata}
    assert model.model_dump(mode="json", by_alias=True) == {
        "omitted": public, "history": [public],
    }
    for value in (model, {"output": [model]}):
        encoded = encode_workflow_result(value)
        assert "_metadata_json" not in encoded.value_json
        decoded = decode_workflow_result(encoded)
        result = decoded if value is model else decoded["output"][0]
        assert isinstance(result["omitted"], ava.Skipped)
        assert result == {"omitted": outcome, "history": [outcome]}
        assert result["omitted"].metadata == outcome.metadata


def test_metadata_is_immutable_and_rejects_lossy_transport():
    metadata = {"nested": ["original"]}
    outcome = ava.skip("no records", metadata)
    metadata["nested"].append("changed")
    outcome.metadata["nested"].append("also changed")
    assert outcome.metadata == {"nested": ["original"]}
    with pytest.raises(TypeError):
        ava.skip("no records", {"value": float("nan")})
    with pytest.raises(TypeError):
        ava.skip("no records", {"value": {1: "not a string key"}})


def test_skip_finalizes_services_and_satisfies_receipt_dependencies(executor, context_type):
    class Services:
        def probe(self, *, request, task):
            return request

        def negotiate(self, *, request, task, probe):
            return probe

        def open(self, *, request, task, negotiation, upstream_receipts):
            return {"node": task.node_slug, "parents": upstream_receipts}

        def materialize_input(self, *, session, input_type, input):
            return input

        def finalize(self, *, session):
            return dict(session)

        def abort(self, *, session, error):
            raise AssertionError("A successful skip must not abort")

        def teardown(self, *, session):
            session.clear()

    @ava.source(num_returns=2)
    def absent():
        return ava.skip("service-managed absence")

    @ava.source(num_returns=2)
    def present():
        return ava.skip("nested"), ava.skip("nested")

    @ava.step
    def downstream():
        return "completed"

    @ava.workflow(context=context_type)
    def flow():
        return (absent() & present()) >> downstream()

    skips, successes = {}, []
    handle = flow().run(
        executor=executor,
        execution_services=ava.ExecutionServicesSpec(service=Services(), request=None),
        hooks=RunHooks(
            on_node_skipped=lambda node, outcome: skips.update({node: outcome}),
            on_node_success=successes.append,
        ),
    )
    assert handle.result(timeout=60) == "completed"
    assert skips == {"absent_1": ava.skip("service-managed absence")}
    assert set(successes) == {"present_1", "downstream_1"}
    receipt, = handle.execution_receipts()
    assert receipt.value == {
        "node": "downstream",
        "parents": (
            {"node": "absent", "parents": ()},
            {"node": "present", "parents": ()},
        ),
    }
