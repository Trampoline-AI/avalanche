"""Typed streams materialize public models across execution and rerun boundaries."""

import pytest

import avalanche as ava
from avalanche._testing.model_stream_helpers import (
    ModelStreamRow as ModelRow,
)
from avalanche._testing.model_stream_helpers import (
    append_passthrough_people,
    collect_model_pairs,
    return_model,
)
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable


@pytest.fixture(params=["local", pytest.param("ray", marks=pytest.mark.ray)])
def executor(request):
    if request.param == "local":
        yield ava.LocalExecutor()
        return
    ray = pytest.importorskip("ray")
    ray.init(num_cpus=2, include_dashboard=False)
    try:
        yield ava.RayExecutor()
    finally:
        ray.shutdown()


@pytest.fixture
def table(tmp_path):
    class Models(IcebergNs):
        ns_config = IcebergNsConfig(name="models", base_location=str(tmp_path / "warehouse"))
        people = IcebergTable(schema=ModelRow)

    ns = Models(
        catalog="model-stream-catalog",
        load_catalog_props={"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db"},
    )
    ns.push()
    return ns.people


def test_model_passthrough_and_lazy_rerun_preserve_order(table, executor):
    load = ava.source(slug="load-people")(append_passthrough_people)
    consume = ava.step(slug="consume-people")(collect_model_pairs)

    @ava.workflow
    def flow():
        return load(people=table) >> consume(people=ava.ModelStream.all(table))

    assert flow().run(executor=executor, run_id="original").result() == [
        (2, "second"),
        (1, "first"),
    ]
    assert flow().run(
        executor=executor,
        run_id="rerun",
        rerun=ava.Rerun(run_id="original", start=["consume-people"], mode="lazy"),
    ).result() == [(2, "second"), (1, "first")]
    assert sorted(table.read_models(), key=lambda row: row.id) == [
        ModelRow(id=1, name="first"),
        ModelRow(id=2, name="second"),
    ]


def test_model_cardinality_failure_does_not_consume_snapshot(table, executor):
    first = table.append(ModelRow(id=7, name="single"))
    second = table.append([ModelRow(id=8, name="left"), ModelRow(id=9, name="right")])
    consume = ava.step(slug="consume-person")(return_model)

    @ava.workflow
    def one():
        return consume(person=ava.ModelStream.one(table, key="typed", mode="append_scan"))

    assert one().run(executor=executor).result() == ModelRow(id=7, name="single")
    with pytest.raises(ValueError):
        one().run(executor=executor).result()
    store = ava.ProgressStore(table, key="typed")
    assert store.get_cursor() == first.snapshot_id
    assert store.list_pending() == [second.snapshot_id]

    @ava.workflow
    def all_rows():
        return consume(person=ava.ModelStream.all(table, key="typed", mode="append_scan"))

    assert all_rows().run(executor=executor).result() == [
        ModelRow(id=8, name="left"),
        ModelRow(id=9, name="right"),
    ]
    assert store.list_pending() == []

    @ava.workflow
    def optional():
        return consume(
            person=ava.ModelStream.one_or_none(table, key="typed", mode="append_scan")
        )

    assert optional().run(executor=executor).result() is None
