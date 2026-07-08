"""Serialization contracts for table handles crossing process boundaries.

Stream steps ship their table handles to executor workers (Ray pickles the
function plus its defaults). Live catalog connections cannot travel; the
handle must carry the recipe to reconnect instead.
"""

from __future__ import annotations

import pickle

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable


class RecordSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


def _make_namespace(tmp_path, *, catalog_uri: str):
    class PickleNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="pickle_ns",
            base_location=str(tmp_path / "warehouse"),
        )
        records = IcebergTable(schema=RecordSchema)

    return PickleNamespace(
        catalog="pickle-catalog",
        load_catalog_props={"type": "sql", "uri": catalog_uri},
    )


@pytest.fixture
def file_backed_namespace(tmp_path):
    ns = _make_namespace(tmp_path, catalog_uri=f"sqlite:///{tmp_path}/catalog.db")
    ns.push()
    return ns


def test_iceberg_table_pickle_roundtrip_reconnects(file_backed_namespace):
    table = file_backed_namespace.records
    table.append(pl.DataFrame({"id": [1], "value": ["a"]}))

    restored = pickle.loads(pickle.dumps(table))

    assert restored.identifier == table.identifier
    assert restored.read().sort("id")["id"].to_list() == [1]
    # The restored handle must support writes too (ProgressStore needs them)
    restored.append(pl.DataFrame({"id": [2], "value": ["b"]}))
    assert restored.read().sort("id")["id"].to_list() == [1, 2]


def test_stream_marker_pickle_roundtrip(file_backed_namespace):
    stream = ava.Stream(
        file_backed_namespace.records, key="pickle_stream", mode="append_scan"
    )

    restored = pickle.loads(pickle.dumps(stream))

    assert restored.key == "pickle_stream"
    assert restored.mode == "append_scan"
    assert restored.table.identifier == file_backed_namespace.records.identifier


def test_in_memory_catalog_rejects_pickling(tmp_path):
    ns = _make_namespace(tmp_path, catalog_uri="sqlite:///:memory:")
    ns.push()

    with pytest.raises(TypeError, match="in-memory"):
        pickle.dumps(ns.records)


def test_catalog_instance_namespace_pickle_roundtrip(tmp_path):
    from pyiceberg.catalog import load_catalog

    catalog = load_catalog(
        "instance-catalog",
        **{"type": "sql", "uri": f"sqlite:///{tmp_path}/instance.db"},
    )

    class InstanceNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="instance_ns",
            base_location=str(tmp_path / "warehouse"),
        )
        records = IcebergTable(schema=RecordSchema)

    ns = InstanceNamespace(catalog=catalog)
    ns.push()
    ns.records.append(pl.DataFrame({"id": [7], "value": ["g"]}))

    restored = pickle.loads(pickle.dumps(ns.records))
    assert restored.read()["id"].to_list() == [7]


@pytest.mark.ray
def test_ray_executor_runs_table_backed_stream_step(tmp_path):
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
        ns = _make_namespace(tmp_path, catalog_uri=f"sqlite:///{tmp_path}/catalog.db")
        ns.push()
        ns.records.append(pl.DataFrame({"id": [1, 2], "value": ["a", "b"]}))

        @ava.step
        def consume(
            df: pl.DataFrame = ava.Stream(ns.records, key="ray_scan", mode="append_scan")
        ):
            return sorted(df["id"].to_list())

        @ava.workflow
        def scan_workflow():
            return consume()

        assert scan_workflow().run(executor=ava.RayExecutor()) == [1, 2]

        store = ava.ProgressStore(ns.records, key="ray_scan")
        assert store.list_pending() == []
    finally:
        ray.shutdown()
