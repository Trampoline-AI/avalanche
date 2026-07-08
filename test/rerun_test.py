from __future__ import annotations

import json
from typing import Any

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche._testing.rerun_helpers import (
    lineage_load_data as _lineage_load_data,
)
from avalanche._testing.rerun_helpers import (
    lineage_process_data as _lineage_process_data,
)
from avalanche._testing.rerun_helpers import (
    lineage_sink as _lineage_sink,
)
from avalanche._testing.rerun_helpers import (
    lineage_split_multireturn as _lineage_split_multireturn,
)
from avalanche._testing.rerun_helpers import (
    lineage_split_pair as _lineage_split_pair,
)
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.operator.hooks import RunHooks
from avalanche.types import LineagedResult, ParamContext


class RowSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


@pytest.fixture
def rerun_ns(tmp_path):
    class RerunNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="rerun-contract",
            base_location=str(tmp_path),
        )
        source = IcebergTable(schema=RowSchema)
        output = IcebergTable(schema=RowSchema)
        no_lineage = IcebergTable(schema=RowSchema, row_lineage=False)

    ns = RerunNamespace(
        catalog="rerun-contract-catalog",
        load_catalog_props={"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}"},
    )
    ns.push()
    return ns


def _rows(*values: str) -> pl.DataFrame:
    return pl.DataFrame(
        {"id": list(range(1, len(values) + 1)), "value": list(values)}
    )


def test_rerun_spec_is_public_and_validates_shape():
    spec = ava.Rerun(run_id="run_1", start=["chunk_docs"])

    assert spec.run_id == "run_1"
    assert spec.start == ("chunk_docs",)
    assert spec.mode == "autorun"
    assert spec.deployment_id is None

    with pytest.raises(ValueError, match="start"):
        ava.Rerun(run_id="run_1", start=[])

    with pytest.raises(ValueError, match="extra"):
        ava.Rerun(run_id="run_1", start=["chunk_docs"], extra=True)

    with pytest.raises(ValueError, match="mode"):
        ava.Rerun(run_id="run_1", start=["chunk_docs"], mode="invalid")


def test_rerun_param_context_preserves_skipped_parent_positions():
    ctx = ParamContext(
        parent_results=[None, "scheduled-parent"],
        param_position=1,
        node_name="join",
        upstream_node_slugs=["skipped", "scheduled"],
        preserve_missing_results=True,
    )

    assert ctx.get_matching_result() == "scheduled-parent"
    assert ctx.get_matching_node_slug() == "scheduled"


def test_workflow_run_rerun_validates_start_slugs_and_injects_context():
    seen: list[tuple[str, tuple[str, ...], str]] = []

    @ava.step(slug="process-docs")
    def process(ctx: ava.RunContext):
        assert ctx.rerun is not None
        seen.append((ctx.rerun.run_id, ctx.rerun.start, ctx.rerun.mode))
        return "processed"

    @ava.workflow
    def rerunnable_workflow():
        return process()

    result = rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="rerun_1",
        rerun=ava.Rerun(run_id="source_1", start=["process-docs"], mode="lazy"),
    )

    assert result == "processed"
    assert seen == [("source_1", ("process-docs",), "lazy")]

    with pytest.raises(ValueError, match="Unknown rerun start slug"):
        rerunnable_workflow().run(
            executor=ava.LocalExecutor(),
            rerun=ava.Rerun(run_id="source_1", start=["missing"]),
        )


def test_rerun_scheduler_lazy_runs_only_start_set_and_autorun_cascades():
    events: list[str] = []

    @ava.source(slug="load")
    def load():
        events.append("load")

    @ava.step(slug="middle")
    def middle():
        events.append("middle")

    @ava.dest(slug="sink")
    def sink():
        events.append("sink")

    @ava.workflow
    def rerunnable_workflow():
        load() >> middle() >> sink()

    rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        rerun=ava.Rerun(run_id="source_1", start=["middle"], mode="lazy"),
    )
    assert events == ["middle"]

    events.clear()
    rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        rerun=ava.Rerun(run_id="source_1", start=["middle"], mode="autorun"),
    )
    assert events == ["middle", "sink"]


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_rerun_scheduler_prunes_skipped_upstreams_on_executors(executor_factory):
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    @ava.source(slug="load")
    def load():
        raise AssertionError("load should be skipped during rerun")

    @ava.step(slug="middle")
    def middle():
        return "middle"

    @ava.dest(slug="sink")
    def sink():
        return "sink"

    @ava.workflow
    def lazy_workflow():
        middle_future = load() >> middle()
        middle_future >> sink()
        return middle_future

    @ava.workflow
    def autorun_workflow():
        return load() >> middle() >> sink()

    executor = executor_factory()
    try:
        assert lazy_workflow().run(
            executor=executor,
            rerun=ava.Rerun(run_id="source_1", start=["middle"], mode="lazy"),
        ) == "middle"
        assert autorun_workflow().run(
            executor=executor,
            rerun=ava.Rerun(run_id="source_1", start=["middle"], mode="autorun"),
        ) == "sink"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()


def test_rerun_stream_reads_source_run_rows_and_bypasses_progress_store(rerun_ns):
    ns = rerun_ns
    source_values = ["alpha", "beta"]

    @ava.source(slug="load-data")
    def load_data(*, source=ns.source):
        return source.append(_rows(*source_values))

    @ava.source(slug="other-data")
    def other_data(*, source=ns.source):
        return source.append(_rows("noise"))

    @ava.step(slug="process-data")
    def process_data(
        df: pl.DataFrame = ava.Stream(
            ns.source, key="source_to_process", mode="append_scan"
        ),
        *,
        output=ns.output,
    ):
        output.append(
            pl.DataFrame(
                {
                    "id": df["id"],
                    "value": df["value"] + "-processed",
                }
            )
        )
        return df["value"].to_list()

    @ava.workflow
    def rerunnable_workflow():
        other_data()
        return load_data() >> process_data()

    assert rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="source_run",
    ) == ["alpha", "beta"]

    store = ava.ProgressStore(ns.source, key="source_to_process")
    cursor_before = store.get_cursor()
    pending_before = store.list_pending()
    source_run_rows = ns.source.read().filter(pl.col("_ava_run_id") == "source_run")
    assert source_run_rows.height == 3
    assert set(source_run_rows["_ava_node_slug"].to_list()) == {"load-data", "other-data"}

    assert rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="rerun_1",
        rerun=ava.Rerun(run_id="source_run", start=["process-data"], mode="lazy"),
    ) == ["alpha", "beta"]

    # Rerun mode is independent of snapshot progress state.
    assert ava.ProgressStore(ns.source, key="source_to_process").get_cursor() == cursor_before
    assert (
        ava.ProgressStore(ns.source, key="source_to_process").list_pending()
        == pending_before
    )

    output_rows = ns.output.read().sort(["_ava_run_id", "id"]).to_dicts()
    rerun_rows = [row for row in output_rows if row["_ava_run_id"] == "rerun_1"]
    assert [row["value"] for row in rerun_rows] == [
        "alpha-processed",
        "beta-processed",
    ]
    for row in rerun_rows:
        assert row["_ava_rerun_of"] == "source_run"
        assert row["_ava_node_slug"] == "process-data"
        assert json.loads(row["_ava_lineage_vector"]) == {
            "load-data": "source_run",
            "process-data": "rerun_1",
        }

    source_values[:] = ["gamma"]
    assert rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="source_rerun",
        rerun=ava.Rerun(run_id="source_run", start=["load-data"], mode="autorun"),
    ) == ["gamma"]

    assert rerunnable_workflow().run(
        executor=ava.LocalExecutor(),
        run_id="rerun_2",
        rerun=ava.Rerun(run_id="source_rerun", start=["process-data"], mode="lazy"),
    ) == ["gamma"]


def test_rerun_stream_requires_row_lineage(rerun_ns):
    ns = rerun_ns
    ns.no_lineage.append(_rows("alpha"))

    @ava.step(slug="process-data")
    def process_data(
        df: pl.DataFrame = ava.Stream(ns.no_lineage),
    ):
        return df.height

    @ava.workflow
    def rerunnable_workflow():
        return process_data()

    with pytest.raises(ValueError, match="row_lineage=True"):
        rerunnable_workflow().run(
            executor=ava.LocalExecutor(),
            rerun=ava.Rerun(run_id="source_run", start=["process-data"]),
        )


def test_sparse_lazy_rerun_of_rerun_resolves_parent_run_rows(rerun_ns):
    ns = rerun_ns

    @ava.source(slug="load-data")
    def load_data(*, source=ns.source):
        return source.append(_rows("alpha", "beta"))

    @ava.step(slug="process-data")
    def process_data(
        df: pl.DataFrame = ava.Stream(ns.source),
        *,
        output=ns.output,
    ):
        output.append(
            pl.DataFrame({"id": df["id"], "value": df["value"] + "-processed"})
        )
        return df["value"].to_list()

    @ava.workflow
    def wf():
        return load_data() >> process_data()

    assert wf().run(executor=ava.LocalExecutor(), run_id="source_run") == [
        "alpha",
        "beta",
    ]

    # Lazy rerun of process-data consumes ns.source but writes nothing to it.
    assert wf().run(
        executor=ava.LocalExecutor(),
        run_id="rerun_1",
        rerun=ava.Rerun(run_id="source_run", start=["process-data"], mode="lazy"),
    ) == ["alpha", "beta"]

    source_rerun_1_rows = ns.source.read().filter(pl.col("_ava_run_id") == "rerun_1")
    assert source_rerun_1_rows.height == 0

    # Rerun-of-rerun from the lazy rerun must still resolve the base input rows
    # by walking the durable rerun ancestry edge, not the sparse payload scan.
    assert wf().run(
        executor=ava.LocalExecutor(),
        run_id="rerun_2",
        rerun=ava.Rerun(run_id="rerun_1", start=["process-data"], mode="lazy"),
    ) == ["alpha", "beta"]


def test_rerun_rejects_skipped_implicit_non_stream_upstream(rerun_ns):
    @ava.source(slug="load")
    def load():
        return "value"

    @ava.step(slug="middle")
    def middle(value):
        return value

    @ava.workflow
    def wf():
        return load() >> middle()

    with pytest.raises(ValueError, match="Stream"):
        wf().run(
            executor=ava.LocalExecutor(),
            rerun=ava.Rerun(run_id="source_run", start=["middle"], mode="lazy"),
        )


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_rerun_lineage_vector_propagates_through_python_args(rerun_ns, executor_factory):
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    ns = rerun_ns

    load_data = ava.source(slug="load-data")(_lineage_load_data)
    process_data = ava.step(slug="process-data")(_lineage_process_data)
    sink = ava.dest(slug="sink")(_lineage_sink)

    @ava.workflow
    def wf():
        return (
            load_data(source=ns.source)
            >> process_data(df=ava.Stream(ns.source))
            >> sink(output=ns.output)
        )

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "ok"
        assert wf().run(
            executor=executor,
            run_id="rerun_run",
            rerun=ava.Rerun(run_id="source_run", start=["process-data"], mode="autorun"),
        ) == "ok"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    sink_rows = (
        ns.output.read()
        .filter((pl.col("_ava_run_id") == "rerun_run") & (pl.col("_ava_node_slug") == "sink"))
    )
    assert sink_rows.height == 1
    vector = json.loads(sink_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["load-data"] == "source_run"
    assert vector["process-data"] == "rerun_run"
    assert vector["sink"] == "rerun_run"

    # The parent-process namespace handles must reflect commits made by the
    # executor (Ray commits from a worker process). A stale handle would read
    # an empty table even though the workflow succeeded.
    source_rows = ns.source.read().filter(pl.col("_ava_run_id") == "source_run")
    assert source_rows.height == 1
    assert source_rows["_ava_node_slug"].to_list() == ["load-data"]


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_rerun_lineage_vector_propagates_through_indexed_single_return_tuple(
    rerun_ns,
    executor_factory,
):
    """A single-return node returning a tuple, indexed downstream via ``pair[0]``.

    Exercises ``_indexed_parent_result``: the whole tuple is wrapped in one
    ``LineagedResult`` envelope, so indexing must preserve the producer lineage
    onto the selected element. Under Ray the parent result is an ObjectRef to
    that envelope, so it must be materialized before indexing.
    """
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    ns = rerun_ns

    split_pair = ava.source(slug="split-pair")(_lineage_split_pair)
    sink = ava.dest(slug="sink")(_lineage_sink)

    @ava.workflow
    def wf():
        pair = split_pair()
        return pair[0] >> sink(output=ns.output)

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "ok"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    sink_rows = ns.output.read().filter(
        (pl.col("_ava_run_id") == "source_run") & (pl.col("_ava_node_slug") == "sink")
    )
    assert sink_rows.height == 1
    assert sink_rows["value"].to_list() == ["alpha-left"]
    vector = json.loads(sink_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["split-pair"] == "source_run"
    assert vector["sink"] == "source_run"


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_rerun_lineage_vector_propagates_through_indexed_multi_return_tuple(
    rerun_ns,
    executor_factory,
):
    """A true multi-return node (num_returns=2) indexed downstream via ``pair[0]``.

    Distinct from the single-return tuple case: under Ray the parent result is a
    tuple of ObjectRefs, so ``_indexed_parent_result`` must materialize the
    selected element ref before binding it into the downstream node.
    """
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    ns = rerun_ns

    split = ava.source(slug="split-multi", num_returns=2)(_lineage_split_multireturn)
    sink = ava.dest(slug="sink")(_lineage_sink)

    @ava.workflow
    def wf():
        pair = split()
        return pair[0] >> sink(output=ns.output)

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "ok"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    sink_rows = ns.output.read().filter(
        (pl.col("_ava_run_id") == "source_run") & (pl.col("_ava_node_slug") == "sink")
    )
    assert sink_rows.height == 1
    assert sink_rows["value"].to_list() == ["left"]
    vector = json.loads(sink_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["split-multi"] == "source_run"
    assert vector["sink"] == "source_run"


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_rerun_lineage_vector_propagates_through_explicit_node_future_arg(
    rerun_ns,
    executor_factory,
):
    """Lineage must propagate when an upstream is passed as an explicit arg.

    ``sink(processed)`` binds the upstream NodeFuture as an explicit positional
    argument rather than via ``>>`` chaining, exercising the explicit-arg
    LineagedResult path across the executor boundary.
    """
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    ns = rerun_ns

    load_data = ava.source(slug="load-data")(_lineage_load_data)
    process_data = ava.step(slug="process-data")(_lineage_process_data)
    sink = ava.dest(slug="sink")(_lineage_sink)

    @ava.workflow
    def wf():
        loaded = load_data(source=ns.source)
        processed = process_data(df=ava.Stream(ns.source))
        loaded >> processed
        return sink(processed, output=ns.output)

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "ok"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    sink_rows = ns.output.read().filter(
        (pl.col("_ava_run_id") == "source_run") & (pl.col("_ava_node_slug") == "sink")
    )
    assert sink_rows.height == 1
    vector = json.loads(sink_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["load-data"] == "source_run"
    assert vector["process-data"] == "source_run"
    assert vector["sink"] == "source_run"


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_lineage_survives_hook_replacement_without_exposing_envelope(
    rerun_ns,
    executor_factory,
):
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    ns = rerun_ns

    load_data = ava.source(slug="load-data")(_lineage_load_data)
    process_data = ava.step(slug="process-data")(_lineage_process_data)
    sink = ava.dest(slug="sink")(_lineage_sink)

    @ava.workflow
    def wf():
        return (
            load_data(source=ns.source)
            >> process_data(df=ava.Stream(ns.source))
            >> sink(output=ns.output)
        )

    hook_saw_envelope: list[bool] = []

    def unwrap_result(node_id: str, value: Any) -> Any:
        hook_saw_envelope.append(isinstance(value, LineagedResult))
        if isinstance(value, pl.DataFrame) and value["value"].to_list() == [
            "alpha-processed"
        ]:
            return pl.DataFrame({"id": [1], "value": ["hooked"]})
        return value

    executor = executor_factory()
    try:
        assert wf().run(
            executor=executor,
            hooks=RunHooks(unwrap_result=unwrap_result),
            run_id="source_run",
        ) == "ok"
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert hook_saw_envelope
    assert not any(hook_saw_envelope)

    sink_rows = ns.output.read().filter(
        (pl.col("_ava_run_id") == "source_run") & (pl.col("_ava_node_slug") == "sink")
    )
    assert sink_rows.height == 1
    assert sink_rows["value"].to_list() == ["hooked"]
    vector = json.loads(sink_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["load-data"] == "source_run"
    assert vector["process-data"] == "source_run"
    assert vector["sink"] == "source_run"
