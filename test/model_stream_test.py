"""Typed, cardinality-aware ModelStream provider contracts."""

from __future__ import annotations

from collections.abc import Iterator

import polars as pl
import pytest

import avalanche as ava
from avalanche._testing.model_stream_helpers import (
    ModelStreamRow as ModelRow,
)
from avalanche._testing.model_stream_helpers import (
    append_passthrough_people,
    append_rerun_people,
    collect_model_names,
    collect_model_pairs,
    return_model,
)
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.runtime import run_with_context
from avalanche.runtime.providers import PROVIDERS


class _ModelTable:
    row_model = ModelRow
    identifier = "models.people"


@pytest.mark.parametrize(
    ("factory", "rows", "expected"),
    [
        (ava.ModelStream.one, [(1, "one")], ModelRow(id=1, name="one")),
        (ava.ModelStream.one_or_none, [], None),
        (ava.ModelStream.one_or_none, [(1, "one")], ModelRow(id=1, name="one")),
        (ava.ModelStream.all, [], []),
        (ava.ModelStream.all, [(2, "second")], [ModelRow(id=2, name="second")]),
        (
            ava.ModelStream.all,
            [(2, "second"), (1, "first")],
            [ModelRow(id=2, name="second"), ModelRow(id=1, name="first")],
        ),
    ],
)
def test_model_stream_materializes_each_cardinality_in_stable_order(factory, rows, expected):
    frame = pl.DataFrame(
        {
            "id": [row[0] for row in rows],
            "name": [row[1] for row in rows],
            "_ava_run_id": ["source-run"] * len(rows),
            "_ava_node_slug": ["load-people"] * len(rows),
        },
        schema={
            "id": pl.Int64,
            "name": pl.String,
            "_ava_run_id": pl.String,
            "_ava_node_slug": pl.String,
        },
    )

    stream = factory(_ModelTable())

    assert stream._materialize(frame, ("load-people",)) == expected


@pytest.mark.parametrize(
    ("factory", "row_count", "expectation"),
    [
        (ava.ModelStream.one, 0, "expected exactly one row; got 0 rows"),
        (ava.ModelStream.one, 2, "expected exactly one row; got 2 rows"),
        (ava.ModelStream.one_or_none, 2, "expected at most one row; got 2 rows"),
    ],
)
def test_model_stream_cardinality_errors_include_available_context(
    factory, row_count, expectation
):
    frame = pl.DataFrame(
        {"id": list(range(row_count)), "name": ["person"] * row_count},
        schema={"id": pl.Int64, "name": pl.String},
    )
    stream = factory(_ModelTable())
    context = ava.RunContext(run_id="run-42", workflow_name="people-workflow")

    with pytest.raises(ValueError) as exc_info:
        run_with_context(
            context,
            stream._materialize,
            frame,
            ("load-people",),
        )

    message = str(exc_info.value)
    assert expectation in message
    assert "table='models.people'" in message
    assert "workflow='people-workflow'" in message
    assert "run_id='run-42'" in message
    assert "source_node='load-people'" in message


def test_model_stream_validation_errors_include_available_context():
    stream = ava.ModelStream.all(_ModelTable())
    context = ava.RunContext(run_id="run-invalid", workflow_name="people-workflow")

    with pytest.raises(ValueError) as exc_info:
        run_with_context(
            context,
            stream._materialize,
            pl.DataFrame({"id": ["not-an-int"], "name": ["invalid"]}),
            ("load-people",),
        )

    message = str(exc_info.value)
    assert "ModelStream failed to validate rows" in message
    assert "table='models.people'" in message
    assert "workflow='people-workflow'" in message
    assert "run_id='run-invalid'" in message
    assert "source_node='load-people'" in message


def test_model_stream_requires_a_model_declared_table_and_has_own_provider():
    class UntypedTable:
        row_model = None
        identifier = "models.untyped"

    with pytest.raises(TypeError, match="pydantic model schema.*models.untyped"):
        ava.ModelStream.all(UntypedTable())

    marker = ava.ModelStream.all(_ModelTable())
    provider = next(provider for provider in PROVIDERS if provider.can_resolve(marker))

    assert provider is ava.ModelStream
    assert repr(marker).startswith("ModelStream.all(")


@pytest.fixture(params=["local", pytest.param("ray", marks=pytest.mark.ray)])
def model_executor(request: pytest.FixtureRequest) -> Iterator[ava.Executor]:
    if request.param == "local":
        yield ava.LocalExecutor()
        return

    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )
    try:
        yield ava.RayExecutor()
    finally:
        ray.shutdown()


@pytest.fixture
def model_namespace(tmp_path):
    class ModelNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="model-stream-contract",
            base_location=str(tmp_path / "warehouse"),
        )
        people = IcebergTable(schema=ModelRow)

    namespace = ModelNamespace(
        catalog="model-stream-catalog",
        load_catalog_props={
            "type": "sql",
            "uri": f"sqlite:///{tmp_path / 'catalog.db'}",
        },
    )
    namespace.push()
    return namespace


def test_model_stream_passthrough_is_consistent_across_executors(
    model_namespace, model_executor
):
    namespace = model_namespace

    load_people = ava.source(slug="load-people")(append_passthrough_people)
    consume_people = ava.step(slug="consume-people")(collect_model_pairs)

    @ava.workflow
    def workflow():
        return load_people(people=namespace.people) >> consume_people(
            people=ava.ModelStream.all(namespace.people)
        )

    assert workflow().run(
        executor=model_executor,
        run_id="passthrough-run",
    ).result() == [(2, "second"), (1, "first")]


def test_model_stream_table_backed_is_consistent_across_executors(
    model_namespace, model_executor
):
    namespace = model_namespace
    appended = namespace.people.append(ModelRow(id=7, name="table-backed"))

    consume_person = ava.step(slug="consume-person")(return_model)

    @ava.workflow
    def workflow():
        return consume_person(
            person=ava.ModelStream.one(
                namespace.people,
                key="model_table_backed",
                mode="append_scan",
            )
        )

    assert workflow().run(executor=model_executor).result() == ModelRow(
        id=7, name="table-backed"
    )
    assert (
        ava.ProgressStore(namespace.people, key="model_table_backed").get_cursor()
        == appended.snapshot_id
    )


def test_model_stream_rerun_is_consistent_across_executors(
    model_namespace, model_executor
):
    namespace = model_namespace

    load_people = ava.source(slug="load-people")(append_rerun_people)
    consume_people = ava.step(slug="consume-people")(collect_model_names)

    @ava.workflow
    def workflow():
        return load_people(people=namespace.people) >> consume_people(
            people=ava.ModelStream.all(namespace.people)
        )

    assert workflow().run(
        executor=model_executor,
        run_id="source-run",
    ).result() == ["third", "fourth"]
    assert workflow().run(
        executor=model_executor,
        run_id="rerun-run",
        rerun=ava.Rerun(run_id="source-run", start=["consume-people"], mode="lazy"),
    ).result() == ["third", "fourth"]
