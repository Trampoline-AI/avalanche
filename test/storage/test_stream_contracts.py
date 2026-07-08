"""Backend-neutral Stream contracts for storage tables."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable
from avalanche.runtime import consume_stream


class StreamSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


@dataclass(frozen=True)
class BackendCase:
    name: str
    table_cls: type
    namespace_cls: type
    namespace_config_cls: type
    namespace_kwargs: Callable[[str], dict[str, Any]]
    requires: tuple[str, ...] = ()


BACKENDS = [
    BackendCase(
        name="iceberg",
        table_cls=IcebergTable,
        namespace_cls=IcebergNs,
        namespace_config_cls=IcebergNsConfig,
        namespace_kwargs=lambda tmpdir: {
            "catalog": "stream-contract-catalog",
            "load_catalog_props": {"type": "sql", "uri": "sqlite:///:memory:"},
        },
    ),
    BackendCase(
        name="lance",
        table_cls=LanceTable,
        namespace_cls=LanceNamespace,
        namespace_config_cls=LanceNamespaceConfig,
        namespace_kwargs=lambda tmpdir: {},
        requires=("lance",),
    ),
]


def _skip_missing_backend(case: BackendCase) -> None:
    for module_name in case.requires:
        pytest.importorskip(module_name)


@pytest.fixture(params=BACKENDS, ids=lambda case: case.name)
def backend(request: pytest.FixtureRequest) -> BackendCase:
    case = request.param
    _skip_missing_backend(case)
    return case


@pytest.fixture
def namespace(backend: BackendCase, tmp_path):
    class StreamNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"stream-{backend.name}",
            base_location=str(tmp_path),
        )
        records = backend.table_cls(schema=StreamSchema)
        dest = backend.table_cls(schema=StreamSchema)
        table_a = backend.table_cls(schema=StreamSchema)
        table_b = backend.table_cls(schema=StreamSchema)
        result = backend.table_cls(schema=StreamSchema)
        no_lineage = backend.table_cls(schema=StreamSchema, row_lineage=False)

    ns = StreamNamespace(**backend.namespace_kwargs(str(tmp_path)))
    ns.push()
    return ns


@pytest.fixture
def table(namespace):
    return namespace.records


def _rows(*ids: int) -> pl.DataFrame:
    return pl.DataFrame({"id": list(ids), "value": [f"value-{i}" for i in ids]})


def _value_rows(values: list[str]) -> pl.DataFrame:
    ids = list(range(1, len(values) + 1))
    return pl.DataFrame({"id": ids, "value": values})


def test_consume_stream_reads_one_data_version_at_a_time(table):
    first = table.append(_rows(1, 2))
    second = table.append(_rows(1))

    with consume_stream(table, key="version_stream", mode="append_scan") as df:
        assert df.sort("id")["id"].to_list() == [1, 2]

    store = ava.ProgressStore(table, key="version_stream")
    assert store.get_cursor() == first.snapshot_id

    with consume_stream(table, key="version_stream", mode="append_scan") as df:
        assert df["id"].to_list() == [1]

    assert store.get_cursor() == second.snapshot_id
    assert store.list_pending() == []


def test_consume_stream_returns_empty_when_no_pending_versions(table):
    table.append(_rows(1))

    with consume_stream(table, key="empty_after_done", mode="append_scan") as df:
        assert df["id"].to_list() == [1]

    with consume_stream(table, key="empty_after_done", mode="append_scan") as df:
        assert df.is_empty()


def test_stream_cursor_survives_new_consumer_instance(table):
    table.append(_rows(1, 2))

    with consume_stream(table, key="persistent_test", mode="append_scan") as df:
        assert df.sort("id")["id"].to_list() == [1, 2]

    assert ava.ProgressStore(table, key="persistent_test").list_pending() == []

    table.append(_rows(3))

    with consume_stream(table, key="persistent_test", mode="append_scan") as df:
        assert df["id"].to_list() == [3]


def test_zero_copy_mode_claims_and_completes_snapshot(namespace):
    ns = namespace

    loaded_snapshot_ids: list[int] = []

    @ava.source
    def load_data(*, source=ns.records):
        result = source.append(_value_rows(["a", "b", "c"]))
        loaded_snapshot_ids.append(result.snapshot_id)
        return result

    @ava.step
    def process_stream(
        df: pl.DataFrame = ava.Stream(ns.records, key="zero_copy_test", mode="append_scan"),
        *,
        dest=ns.dest,
    ):
        assert df["value"].to_list() == ["a", "b", "c"]
        dest.append(df.to_arrow())
        return "processed"

    @ava.workflow
    def test_workflow():
        return load_data() >> process_stream()

    assert test_workflow().run(executor=ava.LocalExecutor()) == "processed"

    store = ava.ProgressStore(ns.records, key="zero_copy_test")
    assert store.get_cursor() == loaded_snapshot_ids[0]
    assert store.list_pending() == []


def test_table_backed_stream_workflow_claims_and_completes_snapshot(namespace):
    ns = namespace
    result = ns.records.append(_value_rows(["x", "y"]))

    @ava.step
    def consume_from_table(
        df: pl.DataFrame = ava.Stream(ns.records, key="table_backed_test", mode="append_scan"),
        *,
        dest=ns.dest,
    ):
        assert df["value"].to_list() == ["x", "y"]
        dest.append(df.to_arrow())
        return "table_backed_done"

    @ava.workflow
    def test_workflow():
        return consume_from_table()

    assert test_workflow().run(executor=ava.LocalExecutor()) == "table_backed_done"

    store = ava.ProgressStore(ns.records, key="table_backed_test")
    assert store.get_cursor() == result.snapshot_id
    assert store.list_pending() == []


def test_failed_zero_copy_snapshot_remains_pending_for_retry(namespace):
    ns = namespace

    loaded_snapshot_ids: list[int] = []

    @ava.source
    def load_data(*, source=ns.records):
        result = source.append(_value_rows(["test"]))
        loaded_snapshot_ids.append(result.snapshot_id)
        return result

    @ava.step
    def failing_process(
        df: pl.DataFrame = ava.Stream(ns.records, key="failure_test", mode="append_scan"),
    ):
        assert df["value"].to_list() == ["test"]
        raise ValueError("Intentional failure for testing")

    @ava.workflow
    def test_workflow():
        return load_data() >> failing_process()

    with pytest.raises(ValueError, match="Intentional failure"):
        test_workflow().run(executor=ava.LocalExecutor())

    snapshot_id = loaded_snapshot_ids[0]
    store = ava.ProgressStore(ns.records, key="failure_test")
    assert store.get_cursor() is None
    assert snapshot_id in store.list_pending()


def test_async_stream_step_failure_remains_pending_for_retry(namespace):
    ns = namespace

    loaded_snapshot_ids: list[int] = []

    @ava.source
    def load_data(*, source=ns.records):
        result = source.append(_value_rows(["async-test"]))
        loaded_snapshot_ids.append(result.snapshot_id)
        return result

    @ava.step
    async def failing_process(
        df: pl.DataFrame = ava.Stream(
            ns.records, key="async_failure_test", mode="append_scan"
        ),
    ):
        await asyncio.sleep(0)
        assert df["value"].to_list() == ["async-test"]
        raise ValueError("Intentional async failure for testing")

    @ava.workflow
    def test_workflow():
        return load_data() >> failing_process()

    with pytest.raises(ValueError, match="Intentional async failure"):
        test_workflow().run(executor=ava.LocalExecutor())

    snapshot_id = loaded_snapshot_ids[0]
    store = ava.ProgressStore(ns.records, key="async_failure_test")
    assert store.get_cursor() is None
    assert snapshot_id in store.list_pending()


def test_claimed_snapshot_prevents_duplicate_processing(table):
    result = table.append(_rows(1))
    store1 = ava.ProgressStore(table, key="duplicate_test")
    store2 = ava.ProgressStore(table, key="duplicate_test")

    assert store1.claim(result.snapshot_id) == result.snapshot_id
    with pytest.raises(RuntimeError, match="cannot be claimed"):
        store2.claim(result.snapshot_id)

    store1.mark_done(result.snapshot_id)


def test_multiple_pending_snapshots_are_processed_atomically(table):
    results = [table.append(_value_rows([value])) for value in ["first", "second", "third"]]

    processed: list[str] = []
    for _ in range(3):
        with consume_stream(
            table, key="atomic_processing_test", mode="append_scan"
        ) as df:
            assert len(df) == 1
            processed.append(df["value"][0])

    assert processed == ["first", "second", "third"]
    store = ava.ProgressStore(table, key="atomic_processing_test")
    assert store.list_pending() == []
    assert store.get_cursor() == results[-1].snapshot_id


def test_failed_table_backed_snapshot_retries_without_data_leakage(table):
    table.append(_value_rows(["first"]))
    table.append(_value_rows(["second"]))
    table.append(_value_rows(["third"]))
    key = "failure_isolation_test"

    with consume_stream(table, key=key, mode="append_scan") as df:
        assert df["value"].to_list() == ["first"]

    with pytest.raises(ValueError, match="Simulated failure"):
        with consume_stream(table, key=key, mode="append_scan") as df:
            assert df["value"].to_list() == ["second"]
            raise ValueError("Simulated failure")

    with consume_stream(table, key=key, mode="append_scan") as df:
        assert df["value"].to_list() == ["second"]

    with consume_stream(table, key=key, mode="append_scan") as df:
        assert df["value"].to_list() == ["third"]

    assert ava.ProgressStore(table, key=key).list_pending() == []


def test_position_based_zero_copy_matching(namespace):
    ns = namespace

    @ava.source(num_returns=2)
    def load_two_tables(*, table_a=ns.table_a, table_b=ns.table_b):
        result_a = table_a.append(_value_rows(["alpha", "beta"]))
        result_b = table_b.append(_value_rows(["gamma", "delta"]))
        return result_a, result_b

    @ava.step
    def combine_streams(
        df_a: pl.DataFrame = ava.Stream(ns.table_a, key="process_a", mode="append_scan"),
        df_b: pl.DataFrame = ava.Stream(ns.table_b, key="process_b", mode="append_scan"),
        *,
        result=ns.result,
    ):
        assert df_a["value"].to_list() == ["alpha", "beta"]
        assert df_b["value"].to_list() == ["gamma", "delta"]
        result.append(
            pl.DataFrame(
                {
                    "id": df_a["id"],
                    "value": df_a["value"] + "_" + df_b["value"],
                }
            )
        )
        return "combined"

    @ava.workflow
    def test_workflow():
        return load_two_tables() >> combine_streams()

    assert test_workflow().run(executor=ava.LocalExecutor()) == "combined"
    assert ns.result.read().sort("id")["value"].to_list() == [
        "alpha_gamma",
        "beta_delta",
    ]


def test_position_matching_with_mixed_results(namespace):
    ns = namespace

    @ava.source(num_returns=3)
    def load_with_mixed_returns(*, table_a=ns.table_a, table_b=ns.table_b):
        result_a = table_a.append(_value_rows(["first"]))
        metadata = "some_metadata"
        result_b = table_b.append(_value_rows(["second"]))
        return result_a, metadata, result_b

    @ava.step
    def process_positioned_streams(
        df_a: pl.DataFrame = ava.Stream(ns.table_a, key="positioned_a", mode="append_scan"),
        metadata: str = None,
        df_b: pl.DataFrame = ava.Stream(ns.table_b, key="positioned_b", mode="append_scan"),
        *,
        result=ns.result,
    ):
        assert df_a["value"].to_list() == ["first"]
        assert metadata == "some_metadata"
        assert df_b["value"].to_list() == ["second"]
        combined_value = f"{df_a['value'][0]}_{df_b['value'][0]}"
        result.append(pl.DataFrame({"id": [1], "value": [combined_value]}))
        return "mixed_done"

    @ava.workflow
    def test_workflow():
        return load_with_mixed_returns() >> process_positioned_streams()

    assert test_workflow().run(executor=ava.LocalExecutor()) == "mixed_done"
    assert ns.result.read()["value"].to_list() == ["first_second"]


def test_run_scoped_stream_default_reads_current_run_rows_only(namespace):
    ns = namespace
    # Rows produced outside any workflow run (no _ava_run_id) must be ignored.
    ns.records.append(_value_rows(["old"]))

    @ava.source(slug="load-data")
    def load_data(*, source=ns.records):
        source.append(_value_rows(["current"]))
        # Return a non-AppendResult so passthrough does not mask the table read.
        return "loaded"

    @ava.step(slug="process-data")
    def process_data(df: pl.DataFrame = ava.Stream(ns.records)):
        return sorted(df["value"].to_list())

    @ava.workflow
    def wf():
        return load_data() >> process_data()

    assert wf().run(executor=ava.LocalExecutor(), run_id="run_1") == ["current"]

    # The run-scoped read leaves an unrelated append-scan cursor untouched: it
    # neither claims nor advances any pending backlog snapshot.
    store = ava.ProgressStore(ns.records, key="run_scoped_check")
    assert store.get_cursor() is None
    # Both appended snapshots (old + current) remain pending for append-scan
    # consumers; run-scoped mode did not drain them.
    assert len(store.list_pending()) == 2


def test_run_scoped_stream_filters_by_upstream_producer_slug(namespace):
    ns = namespace

    @ava.source(slug="load-data")
    def load_data(*, source=ns.records):
        source.append(_value_rows(["wanted"]))
        return "loaded"

    @ava.source(slug="other-data")
    def other_data(*, source=ns.records):
        source.append(_value_rows(["noise"]))
        return "other"

    @ava.step(slug="process-data")
    def process_data(df: pl.DataFrame = ava.Stream(ns.records)):
        return sorted(df["value"].to_list())

    @ava.workflow
    def wf():
        other_data()
        return load_data() >> process_data()

    # Same run, same table, but process-data must see only its upstream
    # producer (load-data) rows, not the unrelated other-data rows.
    assert wf().run(executor=ava.LocalExecutor(), run_id="run_1") == ["wanted"]


def test_run_scoped_stream_requires_row_lineage(namespace):
    ns = namespace

    @ava.source(slug="load-data")
    def load_data(*, source=ns.no_lineage):
        source.append(_value_rows(["x"]))
        return "loaded"

    @ava.step(slug="process-data")
    def process_data(df: pl.DataFrame = ava.Stream(ns.no_lineage)):
        return df.height

    @ava.workflow
    def wf():
        return load_data() >> process_data()

    with pytest.raises(ValueError, match="row_lineage=True"):
        wf().run(executor=ava.LocalExecutor(), run_id="run_1")


def test_run_scoped_stream_passthrough_short_circuits_table_read(namespace):
    ns = namespace

    # Upstream returns an AppendResult, so passthrough short-circuits the durable
    # read. This works even on a table without row lineage because no run-scoped
    # scan is performed.
    @ava.source(slug="load-data")
    def load_data(*, source=ns.no_lineage):
        return source.append(_value_rows(["passthrough"]))

    @ava.step(slug="process-data")
    def process_data(df: pl.DataFrame = ava.Stream(ns.no_lineage)):
        return df["value"].to_list()

    @ava.workflow
    def wf():
        return load_data() >> process_data()

    assert wf().run(executor=ava.LocalExecutor(), run_id="run_1") == ["passthrough"]


def test_consume_stream_rejects_append_scan_without_key(table):
    with pytest.raises(ValueError, match="append_scan streams require key"):
        with consume_stream(table, mode="append_scan"):
            pass


def test_consume_stream_rejects_run_scoped_with_key(table):
    with pytest.raises(ValueError, match="run_scoped streams do not use key"):
        with consume_stream(table, key="oops"):
            pass
