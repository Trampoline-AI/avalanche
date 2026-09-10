"""Core data-integrity contracts shared by Iceberg and Lance."""

import asyncio
import json
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import dataframely as dy
import polars as pl
import pytest
from pydantic import BaseModel

import avalanche as ava
import avalanche.progress as progress_module
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable
from avalanche.runtime import consume_stream


class RecordSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Person(BaseModel):
    id: int
    name: str
    address: Address
    tags: list[str]


@pytest.fixture(params=["iceberg", "lance"])
def namespace(request, tmp_path):
    if request.param == "iceberg":
        namespace_cls, config_cls, table_cls = IcebergNs, IcebergNsConfig, IcebergTable
        kwargs = {
            "catalog": "contract-catalog",
            "load_catalog_props": {"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db"},
        }
    else:
        pytest.importorskip("lance")
        namespace_cls, config_cls, table_cls = LanceNamespace, LanceNamespaceConfig, LanceTable
        kwargs = {}

    class ContractNamespace(namespace_cls):
        ns_config = config_cls(name="contracts", base_location=str(tmp_path / "warehouse"))
        records = table_cls(schema=RecordSchema)
        people = table_cls(schema=Person)
        no_lineage = table_cls(schema=RecordSchema, row_lineage=False)

    ns = ContractNamespace(**kwargs)
    ns.push()
    return ns


@pytest.fixture
def table(namespace):
    return namespace.records


def test_append_formats_snapshot_history_and_filtered_reads(table, namespace):
    assert table.read().is_empty()
    rows = pl.DataFrame(
        {
            "id": pl.Series([2, 1, 3], dtype=pl.Int32),
            "value": ["second", "first", "third"],
        }
    )
    results = [
        table.append(rows.slice(0, 1)),
        table.append(rows.slice(1, 1).to_arrow()),
        table.append(rows.slice(2, 1).to_arrow().to_batches()[0]),
    ]
    assert [result.to_dicts()[0]["value"] for result in results] == rows["value"].to_list()
    assert len({result.snapshot_id for result in results}) == 3
    assert [entry.snapshot_id for entry in table.history()] == [
        result.snapshot_id for result in results
    ]
    namespace.push()
    with pytest.raises(ValueError):
        table.append(pl.DataFrame({"id": ["not an integer"], "value": ["invalid"]}))
    assert table.current_version_id == results[-1].snapshot_id
    assert (
        table.read().select("id", "value").sort("id").to_dicts() == rows.sort("id").to_dicts()
    )
    filtered = table.scan(filter="id > 1", columns=["value"]).to_polars()
    assert filtered.sort("value").to_dicts() == [{"value": "second"}, {"value": "third"}]
    limited = table.scan(filter="id > 1", columns=["value"], limit=1).to_polars()
    assert limited.to_dicts() in ([{"value": "second"}], [{"value": "third"}])
    assert table.append_scan(snapshot_id=results[0].snapshot_id).to_polars()[
        "id"
    ].to_list() == [2]
    namespace.drop(drop_tables=True)
    namespace.push()
    assert table.read().is_empty()


def test_models_survive_reconnection_and_reject_wrong_schema_without_writing(namespace):
    people = [
        Person(
            id=1, name="Ada", address=Address(city="Toronto", zip_code="M5V"), tags=["math"]
        ),
        Person(id=2, name="Grace", address=Address(city="Arlington"), tags=[]),
    ]
    result = namespace.people.append(people[0])
    assert result.one() == people[0]
    restored = pickle.loads(pickle.dumps(namespace.people))
    assert restored.append(people[1:]).to_models() == people[1:]
    assert sorted(namespace.people.read_models(), key=lambda person: person.id) == people

    with pytest.raises(TypeError):
        restored.append(Address(city="wrong model"))
    with pytest.raises(ValueError):
        restored.append([])
    with pytest.raises(TypeError):
        namespace.records.append(people[0])
    assert sorted(restored.read_models(), key=lambda person: person.id) == people
    assert namespace.records.read().is_empty()


def test_concurrent_reconnected_appends_preserve_all_rows_and_versions(table):
    handles = [table, *(pickle.loads(pickle.dumps(table)) for _ in range(7))]
    barrier = threading.Barrier(len(handles))

    def append(target, row_id):
        barrier.wait(timeout=10)
        return target.append(pl.DataFrame({"id": [row_id], "value": [str(row_id)]}))

    with ThreadPoolExecutor(max_workers=len(handles)) as pool:
        results = list(pool.map(append, handles, range(len(handles))))

    assert len({result.snapshot_id for result in results}) == len(handles)
    assert table.read().sort("id")["id"].to_list() == list(range(len(handles)))
    assert {entry.snapshot_id for entry in table.history()} == {
        result.snapshot_id for result in results
    }


@pytest.mark.parametrize("reconnected", [False, True])
@pytest.mark.parametrize("skip_count", [8, 4])
def test_concurrent_skip_and_data_appends_preserve_each_producer(
    table, reconnected, skip_count,
):
    handles = [
        pickle.loads(pickle.dumps(table)) if reconnected else table for _ in range(8)
    ]
    barrier = threading.Barrier(len(handles))

    @ava.source
    def append(index):
        barrier.wait(timeout=10)
        target = handles[index]
        if index < skip_count:
            return target.append(ava.skip("excluded", {"producer": index}))
        return target.append(pl.DataFrame({"id": [index], "value": [str(index)]}))

    @ava.workflow
    def flow():
        return tuple(append(index) for index in range(len(handles)))

    results = flow().run(
        executor=ava.LocalExecutor(max_workers=8), run_id="concurrent",
    ).result(timeout=60)
    assert results[:skip_count] == tuple(
        ava.skip("excluded", {"producer": index}) for index in range(skip_count)
    )
    assert len({result.snapshot_id for result in results[skip_count:]}) == 8 - skip_count
    table.refresh()
    receipts = [
        json.loads(value) for key, value in table.properties.items()
        if key.startswith("avalanche.skip.")
    ]
    assert sorted(receipt["metadata"]["producer"] for receipt in receipts) == list(
        range(skip_count)
    )
    assert len({receipt["node_slug"] for receipt in receipts}) == skip_count
    assert table.read().sort("id")["id"].to_list() == list(range(skip_count, 8))


def test_cursor_transactions_commit_rollback_and_isolate_keys(table):
    cursor = ava.Cursor(table, key="last_id")
    with pytest.raises(RuntimeError):
        cursor.set(1)
    with cursor.transaction():
        cursor.set(1)
    with pytest.raises(ValueError, match="abort"):
        with cursor.transaction():
            cursor.set(2)
            raise ValueError("abort")
    table.refresh()
    assert ava.Cursor(table, key="last_id").get() == "1"
    assert ava.Cursor(table, key="other").get() is None
    with pytest.raises(RuntimeError):
        cursor.set(3)


def test_progress_claim_exclusion_and_expired_lease_recovery(table, monkeypatch):
    now = 1000
    monkeypatch.setattr(progress_module, "time", SimpleNamespace(time=lambda: now))
    result = table.append(pl.DataFrame({"id": [1], "value": ["work"]}))
    first = ava.ProgressStore(table, key="leases", worker_id="first", lease_ttl_seconds=10)
    second = ava.ProgressStore(table, key="leases", worker_id="second", lease_ttl_seconds=10)
    assert first.claim_next_pending() == result.snapshot_id
    assert second.claim_next_pending() is None
    with pytest.raises(RuntimeError):
        second.claim(result.snapshot_id)
    now += 11
    assert second.claim_next_pending() == result.snapshot_id
    second.mark_done(result.snapshot_id)
    assert second.advance_cursor() == result.snapshot_id
    assert first.list_pending() == []
    assert [entry.snapshot_id for entry in table.history()] == [result.snapshot_id]


def test_progress_cursor_cannot_skip_pending_or_retryable_failure(table):
    snapshots = [
        table.append(pl.DataFrame({"id": [row_id], "value": [str(row_id)]})).snapshot_id
        for row_id in range(3)
    ]
    store = ava.ProgressStore(table, key="ordered", max_attempts=2, max_done_history=1)
    for snapshot in (snapshots[0], snapshots[2]):
        store.claim(snapshot)
        store.mark_done(snapshot)
    assert store.advance_cursor() == snapshots[0]
    store.claim(snapshots[1])
    store.mark_failed(snapshots[1], error="retryable")
    assert store.advance_cursor() is None
    assert store.get_cursor() == snapshots[0]
    assert store.list_pending() == [snapshots[1]]
    store.claim(snapshots[1])
    store.mark_failed(snapshots[1], error="quarantined")
    assert store.advance_cursor() == snapshots[2]
    assert store.list_pending() == []
    store.reset()
    assert store.get_cursor() is None
    assert store.list_pending() == snapshots


def test_append_stream_retries_failed_version_without_leaking_later_rows(table):
    snapshots = [
        table.append(pl.DataFrame({"id": [1], "value": [value]})).snapshot_id
        for value in ("first", "second", "third")
    ]
    with consume_stream(table, key="ordered", mode="append_scan") as frame:
        assert frame["value"].to_list() == ["first"]
    with pytest.raises(ValueError, match="retry"):
        with consume_stream(table, key="ordered", mode="append_scan") as frame:
            assert frame["value"].to_list() == ["second"]
            raise ValueError("retry")
    assert ava.ProgressStore(table, key="ordered").get_cursor() == snapshots[0]
    for value in ("second", "third"):
        with consume_stream(table, key="ordered", mode="append_scan") as frame:
            assert frame["value"].to_list() == [value]
    assert ava.ProgressStore(table, key="ordered").get_cursor() == snapshots[-1]
    with consume_stream(table, key="ordered", mode="append_scan") as frame:
        assert frame.is_empty()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_passthrough_failure_leaves_snapshot_available_for_retry(table, asynchronous):
    @ava.source
    def produce():
        return table.append(pl.DataFrame({"id": [1], "value": ["retry me"]}))

    def fail(frame=ava.Stream(table, key="async_retry", mode="append_scan")):
        assert frame["value"].to_list() == ["retry me"]
        raise ValueError("consumer failed")

    async def async_fail(frame=ava.Stream(table, key="async_retry", mode="append_scan")):
        await asyncio.sleep(0)
        fail(frame)

    consume = ava.step(async_fail if asynchronous else fail)

    @ava.workflow
    def flow():
        return produce() >> consume()

    with pytest.raises(ValueError, match="consumer failed"):
        flow().run(executor=ava.LocalExecutor()).result()
    assert ava.ProgressStore(table, key="async_retry").get_cursor() is None
    with consume_stream(table, key="async_retry", mode="append_scan") as frame:
        assert frame["value"].to_list() == ["retry me"]
    assert ava.ProgressStore(table, key="async_retry").list_pending() == []


def test_run_scoped_stream_isolates_run_and_producer_lineage(table):
    table.append(pl.DataFrame({"id": [0], "value": ["old"]}))

    @ava.source(slug="wanted")
    def produce():
        table.append(pl.DataFrame({"id": [1], "value": ["wanted"]}))
        return "force durable read"

    @ava.source(slug="noise")
    def noise():
        table.append(pl.DataFrame({"id": [2], "value": ["noise"]}))

    @ava.step
    def consume(frame=ava.Stream(table)):
        return frame["value"].to_list()

    @ava.workflow
    def flow():
        noise()
        return produce() >> consume()

    for run_id in ("first-run", "second-run"):
        assert flow().run(executor=ava.LocalExecutor(), run_id=run_id).result() == ["wanted"]
    stored = table.read().filter(pl.col("_ava_run_id") == "second-run")
    wanted = stored.filter(pl.col("_ava_node_slug") == "wanted").to_dicts()[0]
    assert json.loads(wanted["_ava_lineage_vector"]) == {"wanted": "second-run"}
    assert ava.ProgressStore(table, key="backlog").get_cursor() is None
    assert len(ava.ProgressStore(table, key="backlog").list_pending()) == 5


@pytest.mark.parametrize("passthrough", [True, False])
def test_run_scoped_reads_require_lineage_unless_passed_through(namespace, passthrough):
    table = namespace.no_lineage

    @ava.source
    def produce():
        result = table.append(pl.DataFrame({"id": [1], "value": ["current"]}))
        return result if passthrough else "force durable read"

    @ava.step
    def consume(frame=ava.Stream(table)):
        return frame["value"].to_list()

    @ava.workflow
    def flow():
        return produce() >> consume()

    if passthrough:
        assert flow().run(executor=ava.LocalExecutor()).result() == ["current"]
    else:
        with pytest.raises(ValueError, match="row_lineage"):
            flow().run(executor=ava.LocalExecutor()).result()
