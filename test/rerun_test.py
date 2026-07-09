from __future__ import annotations

import json
from functools import partial
from typing import Any

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche._testing.rerun_helpers import (
    RerunSelectorInput,
    explicit_non_stream_consume,
    explicit_non_stream_load,
    explicit_selector_combine,
    explicit_selector_consume,
    explicit_selector_load_left,
    explicit_selector_load_right,
    explicit_selector_split,
    explicit_selector_value,
    keyword_only_selector_value,
    logical_multireturn_consume,
    logical_multireturn_sibling,
    logical_multireturn_split,
    positional_only_selector_consume,
    selector_end,
    unindexed_mixed_consume,
    unindexed_mixed_multireturn,
    unindexed_mixed_single_return_list,
    unindexed_mixed_single_return_tuple,
    varargs_selector_consume,
)
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


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_stream_selector_preserves_trailing_positional_arg_with_base_input(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    load_left = ava.source(slug="load-left")(explicit_selector_load_left)
    consume = ava.step(slug="consume")(explicit_selector_consume)

    @ava.workflow(input=RerunSelectorInput)
    def wf():
        loaded = load_left(source=ns.source)
        return consume(
            loaded,
            "!",
            df=ava.Stream(ns.source),
            output=ns.output,
        )

    executor = executor_factory()
    try:
        assert wf().run(
            executor=executor,
            run_id="source_run",
            input={"suffix": "source"},
        ) == ["left!source"]
        assert wf().run(
            executor=executor,
            run_id="rerun_run",
            input={"suffix": "rerun"},
            rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
        ) == ["left!rerun"]
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    rerun_rows = ns.output.read().filter(pl.col("_ava_run_id") == "rerun_run")
    assert rerun_rows["value"].to_list() == ["left!rerun"]
    vector = json.loads(rerun_rows["_ava_lineage_vector"].to_list()[0])
    assert vector["load-left"] == "source_run"
    assert vector["consume"] == "rerun_run"


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_varargs_selector_reconstructs_injected_slots_and_rejects_indexed_rerun(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(varargs_selector_consume)

    @ava.workflow(input=RerunSelectorInput)
    def wf():
        pair = split(source=ns.source)
        return consume("pre", pair[1], "post", df=ava.Stream(ns.source))

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(
            executor=executor,
            run_id="source_run",
            input={"suffix": "!"},
        ) == ("pre", "right", ("post",), "!")
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                input={"suffix": "!"},
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_positional_only_input_and_stream_injection_without_varargs(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(positional_only_selector_consume)

    @ava.workflow(input=RerunSelectorInput)
    def wf():
        pair = split(source=ns.source)
        return pair[1] >> consume(df=ava.Stream(ns.source))

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(
            executor=executor,
            run_id="source_run",
            input={"suffix": "!"},
        ) == ("right", "!")
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                input={"suffix": "!"},
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_unindexed_multireturn_mixed_stream_python_rejects_lazy_rerun(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(unindexed_mixed_multireturn)
    consume = ava.step(slug="consume")(unindexed_mixed_consume)

    @ava.workflow
    def wf():
        return split(source=ns.source) >> consume(df=ava.Stream(ns.source))

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(executor=executor, run_id="source_run") == "stream+ordinary"
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize(
    "producer",
    [unindexed_mixed_single_return_tuple, unindexed_mixed_single_return_list],
)
def test_single_return_container_mixed_stream_python_rejects_lazy_rerun(
    rerun_ns,
    producer,
):
    ns = rerun_ns
    split = ava.source(slug="split")(producer)
    consume = ava.step(slug="consume")(unindexed_mixed_consume)

    @ava.workflow
    def wf():
        return split(source=ns.source) >> consume(df=ava.Stream(ns.source))

    submitted: list[str] = []
    assert wf().run(executor=ava.LocalExecutor(), run_id="source_run") == "stream+ordinary"
    with pytest.raises(ValueError, match="ambiguous single-return container"):
        wf().run(
            executor=ava.LocalExecutor(),
            run_id="rerun_run",
            hooks=RunHooks(on_node_start=submitted.append),
            rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
        )

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_unindexed_true_multireturn_expands_before_mixed_slot_binding(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(logical_multireturn_split)
    sibling = ava.source(slug="sibling")(logical_multireturn_sibling)
    consume = ava.step(slug="consume")(logical_multireturn_consume)

    @ava.workflow
    def wf():
        parents = split(source=ns.source) & sibling()
        return parents >> consume(middle=ava.Stream(ns.source))

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(executor=executor, run_id="source_run") == (
            "left",
            "middle",
            "other",
        )
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_stream_selectors_preserve_reordered_keyword_mapping(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    load_left = ava.source(slug="load-left")(explicit_selector_load_left)
    load_right = ava.source(slug="load-right")(explicit_selector_load_right)
    combine_fn = partial(
        explicit_selector_combine,
        left_df=ava.Stream(ns.source),
        right_df=ava.Stream(ns.source),
    )
    combine_fn.__name__ = "explicit_selector_combine"
    combine = ava.step(slug="combine")(combine_fn)

    @ava.workflow
    def wf():
        left_ref = load_left(source=ns.source)
        # Serialize same-table appends while keeping distinct producer slugs.
        # One table ensures neither identity matching nor fallback reads can
        # hide a lost parameter-name -> NodeFuture selector mapping.
        right_ref = load_right(left_ref, source=ns.source)
        return combine(right_df=right_ref, left_df=left_ref)

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "left+right"
        assert (
            wf().run(
                executor=executor,
                run_id="rerun_run",
                rerun=ava.Rerun(run_id="source_run", start=["combine"], mode="lazy"),
            )
            == "left+right"
        )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()


@pytest.mark.parametrize("binding_style", ["positional", "keyword"])
def test_unindexed_explicit_multireturn_stream_selector_rejects_lazy_rerun(
    rerun_ns,
    binding_style,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)

    @ava.step(slug="consume")
    def consume(df=ava.Stream(ns.source)):
        return df["value"].to_list()[0]

    @ava.workflow
    def wf():
        pair = split(source=ns.source)
        if binding_style == "positional":
            return consume(pair)
        return consume(df=pair)

    submitted: list[str] = []
    assert wf().run(executor=ava.LocalExecutor(), run_id="source_run") == "left"
    with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
        wf().run(
            executor=ava.LocalExecutor(),
            run_id="rerun_run",
            hooks=RunHooks(on_node_start=submitted.append),
            rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
        )

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
@pytest.mark.parametrize("binding_style", ["explicit", "chain"])
def test_stream_selector_preserves_live_indexed_multi_return_and_rejects_rerun(
    rerun_ns,
    executor_factory,
    binding_style,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(explicit_selector_value)

    @ava.workflow
    def wf():
        refs = split(source=ns.source)
        if binding_style == "explicit":
            return consume(refs[1], df=ava.Stream(ns.source))
        return refs[1] >> consume(df=ava.Stream(ns.source))

    executor = executor_factory()
    try:
        assert wf().run(executor=executor, run_id="source_run") == "right"
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_keyword_only_stream_chain_selects_index_and_rejects_rerun_before_submission(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(keyword_only_selector_value)

    @ava.workflow
    def wf():
        pair = split(source=ns.source)
        return pair[1] >> consume(df=ava.Stream(ns.source))

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(executor=executor, run_id="source_run") == "right"
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_indexed_stream_into_downstream_chain_targets_registered_start(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(keyword_only_selector_value)
    end = ava.step(slug="end")(selector_end)

    @ava.workflow
    def wf():
        pair = split(source=ns.source)
        return pair[1] >> (consume(df=ava.Stream(ns.source)) >> end())

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(executor=executor, run_id="source_run") == "right"
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


@pytest.mark.parametrize("executor_factory", [ava.LocalExecutor, ava.RayExecutor])
def test_parallel_stream_selectors_preserve_index_order_and_reject_rerun(
    rerun_ns,
    executor_factory,
):
    ns = rerun_ns
    split = ava.source(slug="split", num_returns=2)(explicit_selector_split)
    consume = ava.step(slug="consume")(explicit_selector_combine)

    @ava.workflow
    def wf():
        pair = split(source=ns.source)
        return (pair[1] & pair[0]) >> consume(
            left_df=ava.Stream(ns.source),
            right_df=ava.Stream(ns.source),
        )

    executor = executor_factory()
    submitted: list[str] = []
    try:
        assert wf().run(executor=executor, run_id="source_run") == "right+left"
        with pytest.raises(ValueError, match="indexed Stream selectors cannot replay"):
            wf().run(
                executor=executor,
                run_id="rerun_run",
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["consume"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


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
def test_rerun_rejects_skipped_explicit_non_stream_upstream(
    executor_factory,
):
    load = ava.source(slug="load")(explicit_non_stream_load)
    middle = ava.step(slug="middle")(explicit_non_stream_consume)

    @ava.workflow
    def wf():
        loaded = load()
        return middle(loaded)

    executor = executor_factory()
    submitted = []
    try:
        with pytest.raises(ValueError, match="Stream"):
            wf().run(
                executor=executor,
                hooks=RunHooks(on_node_start=submitted.append),
                rerun=ava.Rerun(run_id="source_run", start=["middle"], mode="lazy"),
            )
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    assert submitted == []


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
