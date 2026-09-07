"""Reruns select work without losing producer identity or replaying unrelated rows."""

import json
from functools import partial

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche._testing.rerun_helpers import (
    RerunSelectorInput,
    explicit_selector_combine,
    explicit_selector_consume,
    explicit_selector_load_left,
    explicit_selector_load_right,
    explicit_selector_split,
    lineage_load_data,
    lineage_process_data,
    lineage_sink,
)
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from runtime.operator.hooks import RunHooks


class RowSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


@pytest.fixture
def rerun_ns(tmp_path):
    class RerunNamespace(IcebergNs):
        ns_config = IcebergNsConfig(name="rerun-contract", base_location=str(tmp_path))
        source = IcebergTable(schema=RowSchema)
        output = IcebergTable(schema=RowSchema)
        no_lineage = IcebergTable(schema=RowSchema, row_lineage=False)

    ns = RerunNamespace(
        catalog="rerun-contract-catalog",
        load_catalog_props={"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}"},
    )
    ns.push()
    return ns


def _rows(*values):
    return pl.DataFrame({"id": list(range(1, len(values) + 1)), "value": list(values)})


def test_lazy_rerun_prunes_returns_while_autorun_cascades():
    events = []

    @ava.source
    def load():
        raise AssertionError("rerun must not execute skipped upstream")

    @ava.step(slug="middle")
    def middle(ctx: ava.RunContext):
        events.append("middle")
        return ctx.rerun.run_id

    @ava.dest
    def sink(value):
        events.append("sink")
        return value + "-saved"

    @ava.workflow
    def flow():
        selected = load() >> middle()
        final = selected >> sink()
        return selected, final

    workflow = flow()
    executor = ava.LocalExecutor()
    assert workflow.run(
        executor=executor,
        rerun=ava.Rerun(run_id="source-run", start=["middle"], mode="lazy"),
    ).result(timeout=5) == ("source-run", None)
    assert events == ["middle"]

    events.clear()
    assert workflow.run(
        executor=executor,
        rerun=ava.Rerun(run_id="source-run", start=["middle"], mode="autorun"),
    ).result(timeout=5) == ("source-run", "source-run-saved")
    assert events == ["middle", "sink"]
    with pytest.raises(ValueError, match="Unknown rerun start slug"):
        workflow.run(
            executor=executor,
            rerun=ava.Rerun(run_id="source-run", start=["missing"]),
        ).result(timeout=5)


def test_replay_filters_producers_preserves_progress_and_follows_sparse_ancestry(rerun_ns):
    ns = rerun_ns
    source_values = ["alpha", "beta"]

    @ava.source(slug="load-data")
    def load(*, source=ns.source):
        return source.append(_rows(*source_values))

    @ava.step(slug="process-data")
    def process(
        df=ava.Stream(ns.source, key="source_to_process", mode="append_scan"),
        *,
        output=ns.output,
    ):
        output.append(df.select("id", (pl.col("value") + "-processed").alias("value")))
        return df["value"].to_list()

    @ava.workflow
    def flow():
        return load() >> process()

    executor = ava.LocalExecutor()
    assert flow().run(executor=executor, run_id="source-run").result() == ["alpha", "beta"]

    # Same run, different producer: filtering only by run id would replay this noise.
    @ava.source(slug="other-data")
    def noise(*, source=ns.source):
        return source.append(_rows("noise"))

    @ava.workflow
    def unrelated():
        noise()

    unrelated().run(executor=executor, run_id="source-run").result()
    store = ava.ProgressStore(ns.source, key="source_to_process")
    cursor, pending = store.get_cursor(), store.list_pending()

    for run_id, parent in (("rerun-1", "source-run"), ("rerun-2", "rerun-1")):
        assert flow().run(
            executor=executor,
            run_id=run_id,
            rerun=ava.Rerun(run_id=parent, start=["process-data"], mode="lazy"),
        ).result() == ["alpha", "beta"]
        output = ns.output.read().filter(pl.col("_ava_run_id") == run_id).sort("id")
        assert output["value"].to_list() == ["alpha-processed", "beta-processed"]
        assert output["_ava_rerun_of"].to_list() == [parent, parent]
        assert [json.loads(value) for value in output["_ava_lineage_vector"]] == [
            {"load-data": "source-run", "process-data": run_id},
            {"load-data": "source-run", "process-data": run_id},
        ]
    assert store.get_cursor() == cursor
    assert store.list_pending() == pending
    assert ns.source.read().filter(pl.col("_ava_run_id") == "rerun-1").is_empty()

    source_values[:] = ["gamma"]
    assert flow().run(
        executor=executor,
        run_id="new-source",
        rerun=ava.Rerun(run_id="source-run", start=["load-data"]),
    ).result() == ["gamma"]


def test_stream_selector_keeps_input_injection_and_trailing_positional_argument(rerun_ns):
    ns = rerun_ns
    load = ava.source(slug="load-left")(explicit_selector_load_left)
    consume = ava.step(slug="consume")(explicit_selector_consume)

    @ava.workflow(input=RerunSelectorInput)
    def flow():
        return consume(load(source=ns.source), "!", df=ava.Stream(ns.source), output=ns.output)

    executor = ava.LocalExecutor()
    assert flow().run(
        executor=executor, run_id="source-run", input={"suffix": "source"}
    ).result() == ["left!source"]
    assert flow().run(
        executor=executor,
        run_id="rerun",
        input={"suffix": "rerun"},
        rerun=ava.Rerun(run_id="source-run", start=["consume"], mode="lazy"),
    ).result() == ["left!rerun"]
    rows = ns.output.read().filter(pl.col("_ava_run_id") == "rerun")
    assert rows["value"].to_list() == ["left!rerun"]
    assert json.loads(rows["_ava_lineage_vector"][0]) == {
        "load-left": "source-run",
        "consume": "rerun",
    }


def test_reordered_keyword_selectors_replay_their_own_producer(rerun_ns):
    ns = rerun_ns
    left = ava.source(slug="left")(explicit_selector_load_left)
    right = ava.source(slug="right")(explicit_selector_load_right)
    combine_fn = partial(
        explicit_selector_combine,
        left_df=ava.Stream(ns.source),
        right_df=ava.Stream(ns.source),
    )
    combine_fn.__name__ = "combine"
    combine = ava.step(combine_fn)

    @ava.workflow
    def flow():
        left_ref = left(source=ns.source)
        right_ref = right(left_ref, source=ns.source)
        return combine(right_df=right_ref, left_df=left_ref)

    executor = ava.LocalExecutor()
    assert flow().run(executor=executor, run_id="source-run").result() == "left+right"
    assert (
        flow()
        .run(
            executor=executor,
            rerun=ava.Rerun(run_id="source-run", start=["combine"], mode="lazy"),
        )
        .result()
        == "left+right"
    )


def test_indexed_streams_work_live_but_ambiguous_replay_fails_before_submission(rerun_ns):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    combine = ava.step(slug="combine")(explicit_selector_combine)

    @ava.workflow
    def flow():
        pair = split(source=ns.source)
        return (pair[1] & pair[0]) >> combine(
            left_df=ava.Stream(ns.source), right_df=ava.Stream(ns.source)
        )

    executor = ava.LocalExecutor()
    assert flow().run(executor=executor, run_id="source-run").result() == "right+left"
    started = []
    with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
        flow().run(
            executor=executor,
            hooks=RunHooks(on_node_start=started.append),
            rerun=ava.Rerun(run_id="source-run", start=["combine"], mode="lazy"),
        ).result()
    assert started == []


def test_skipped_python_input_is_not_silently_replaced_with_none():
    @ava.source
    def load():
        return "data"

    @ava.step
    def consume(value):
        raise AssertionError("invalid rerun must fail before invoking user code")

    @ava.workflow
    def flow():
        return consume(load())

    with pytest.raises(ValueError, match="Stream"):
        flow().run(
            executor=ava.LocalExecutor(),
            rerun=ava.Rerun(run_id="source-run", start=["consume"], mode="lazy"),
        ).result()


def test_rerun_cannot_replay_a_table_without_row_lineage(rerun_ns):
    @ava.step
    def consume(df=ava.Stream(rerun_ns.no_lineage)):
        raise AssertionError("untraceable input must not reach user code")

    @ava.workflow
    def flow():
        return consume()

    with pytest.raises(ValueError, match="row_lineage=True"):
        flow().run(
            executor=ava.LocalExecutor(),
            rerun=ava.Rerun(run_id="source-run", start=["consume"]),
        ).result()


@pytest.mark.ray
def test_ray_rerun_lineage_survives_hook_replacement_and_worker_commits(rerun_ns):
    ray = pytest.importorskip("ray")
    owns_runtime = not ray.is_initialized()
    if owns_runtime:
        ray.init(num_cpus=2, include_dashboard=False)
    ns = rerun_ns
    load = ava.source(slug="load")(lineage_load_data)
    process = ava.step(slug="process")(lineage_process_data)
    sink = ava.dest(slug="sink")(lineage_sink)

    @ava.workflow
    def flow():
        produced = load(source=ns.source)
        processed = process(df=ava.Stream(ns.source))
        produced >> processed
        return sink(processed, output=ns.output)

    def replace_processed(node_id, value):
        if node_id == "lineage_process_data_1":
            return value.with_columns(pl.lit("hooked").alias("value"))
        return value

    try:
        executor = ava.RayExecutor()
        assert flow().run(executor=executor, run_id="source-run").result(timeout=30) == "ok"
        assert (
            flow()
            .run(
                executor=executor,
                run_id="rerun",
                hooks=RunHooks(unwrap_result=replace_processed),
                rerun=ava.Rerun(run_id="source-run", start=["process"], mode="autorun"),
            )
            .result(timeout=30)
            == "ok"
        )
        rows = ns.output.read().filter(pl.col("_ava_run_id") == "rerun")
        assert rows["value"].to_list() == ["hooked"]
        assert json.loads(rows["_ava_lineage_vector"][0]) == {
            "load": "source-run",
            "process": "rerun",
            "sink": "rerun",
        }
        assert ns.source.read()["value"].to_list() == ["alpha"]
    finally:
        if owns_runtime:
            ray.shutdown()
