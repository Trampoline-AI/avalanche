"""Iceberg-specific catalog lifetime and native-schema boundaries."""

import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

import polars as pl
import pytest
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType

from avalanche import Cursor
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable


@pytest.fixture(params=["properties", "catalog"])
def namespace(request, tmp_path):
    class Records(IcebergNs):
        ns_config = IcebergNsConfig(name="records", base_location=str(tmp_path))
        rows = IcebergTable(schema=Schema(NestedField(1, "value", StringType(), required=True)))

    properties = {"type": "sql", "uri": "sqlite:///:memory:"}
    if request.param == "catalog":
        catalog = load_catalog("memory", **properties)
        catalog.create_namespace("caller-owned")
        ns = Records(catalog=catalog)
        assert ("caller-owned",) in ns.catalog.list_namespaces()
    else:
        ns = Records(catalog="memory", load_catalog_props=properties)
    ns.push()
    ns.rows.append(pl.DataFrame({"value": ["survives threads"]}))
    return ns


def test_in_memory_catalog_survives_sequential_and_concurrent_threads(namespace):
    def read_rows():
        return namespace.rows.read()["value"].to_list()

    for _ in range(7):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read_rows).result(timeout=10) == ["survives threads"]

    barrier = threading.Barrier(7)

    def concurrent_read(_):
        barrier.wait(timeout=10)
        return read_rows()

    with ThreadPoolExecutor(max_workers=7) as pool:
        assert list(pool.map(concurrent_read, range(7))) == [["survives threads"]] * 7
    with pytest.raises(TypeError, match="in-memory"):
        pickle.dumps(namespace.rows)


def test_in_memory_catalog_serializes_connection_leases(namespace):
    first_lease = namespace.catalog.engine.connect()
    checkout_started = threading.Event()
    second_acquired = threading.Event()

    def acquire_second():
        checkout_started.set()
        with namespace.catalog.engine.connect():
            second_acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(acquire_second)
        try:
            assert checkout_started.wait(5)
            assert not second_acquired.wait(0.2)
        finally:
            first_lease.close()
        pending.result(timeout=5)
    assert second_acquired.is_set()


def test_conflict_timeout_does_not_report_or_persist_success(namespace, monkeypatch):
    def fail_append(_data):
        raise CommitFailedException("conflict")

    monkeypatch.setattr(namespace.rows._table, "append", fail_append)
    monkeypatch.setattr("avalanche.iceberg.table._APPEND_RETRY_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(CommitFailedException):
        namespace.rows.append(pl.DataFrame({"value": ["not committed"]}))
    assert namespace.rows.read()["value"].to_list() == ["survives threads"]


def test_cursor_and_rows_commit_or_rollback_together(namespace):
    cursor = Cursor(namespace.rows, key="last_row")
    with cursor.transaction() as tx:
        tx.append(pl.DataFrame({"value": ["committed"]}).to_arrow())
        cursor.set(1)
    with pytest.raises(ValueError, match="abort"):
        with cursor.transaction() as tx:
            tx.append(pl.DataFrame({"value": ["aborted"]}).to_arrow())
            cursor.set(2)
            raise ValueError("abort")
    namespace.rows.refresh()
    assert cursor.get() == "1"
    assert sorted(namespace.rows.read()["value"].to_list()) == ["committed", "survives threads"]
