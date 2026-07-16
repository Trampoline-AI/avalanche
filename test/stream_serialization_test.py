"""Serialization contracts for table handles crossing process boundaries.

Stream steps ship their table handles to executor workers (Ray pickles the
function plus its defaults). Live catalog connections cannot travel; the
handle must carry the recipe to reconnect instead.
"""

from __future__ import annotations

import pickle
from typing import Any

import dataframely as dy
import polars as pl
import pytest
from pydantic import BaseModel

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


class RecordModel(BaseModel):
    id: int
    value: str


def test_iceberg_model_table_pickle_roundtrip_keeps_row_model(tmp_path):
    class ModelPickleNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="pickle_model_ns",
            base_location=str(tmp_path / "warehouse"),
        )
        rows = IcebergTable(schema=RecordModel)

    ns = ModelPickleNamespace(
        catalog="pickle-model-catalog",
        load_catalog_props={"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db"},
    )
    ns.push()

    restored = pickle.loads(pickle.dumps(ns.rows))

    assert restored.row_model is RecordModel
    result = restored.append(RecordModel(id=1, value="a"))
    assert result.one() == RecordModel(id=1, value="a")
    assert restored.read_models() == [RecordModel(id=1, value="a")]


def test_lance_model_table_pickle_roundtrip_keeps_row_model(tmp_path):
    pytest.importorskip("lance")
    from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable

    class ModelLanceNamespace(LanceNamespace):
        ns_config = LanceNamespaceConfig(
            name="pickle_model_lance",
            base_location=str(tmp_path),
        )
        rows = LanceTable(schema=RecordModel)

    ns = ModelLanceNamespace()
    ns.push()

    restored = pickle.loads(pickle.dumps(ns.rows))

    assert restored.row_model is RecordModel
    result = restored.append(RecordModel(id=1, value="a"))
    assert result.one() == RecordModel(id=1, value="a")
    assert restored.read_models() == [RecordModel(id=1, value="a")]


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

        assert scan_workflow().run(executor=ava.RayExecutor()).result() == [1, 2]

        store = ava.ProgressStore(ns.records, key="ray_scan")
        assert store.list_pending() == []
    finally:
        ray.shutdown()


@pytest.mark.ray
def test_ray_stream_passthrough_deferred_upstream_num_cpus_1(tmp_path):
    """Ray Stream passthrough must run under num_cpus=1 without deadlock.

    Guards the control/data split: the producer returns an AppendResult, which
    the Ray task wrapper normalizes into an off-driver AppendResultHandle. The
    consumer Stream defers resolution into its own worker (DeferredStreamUpstream
    carried in the wrapper closure). This proves:
    - closure-carried / nested ObjectRefs do not deadlock the scheduler even
      with a single worker slot;
    - worker-side deferred resolution actually yields the passthrough rows;
    - the scheduler gates the consumer on parent completion (no start before the
      producer finishes), so the closure-captured ref never blocks a slot;
    - real Ray serialization of the handle/carrier round-trips correctly.
    """
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_cpus=1,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )

    try:
        ns = _make_namespace(tmp_path, catalog_uri=f"sqlite:///{tmp_path}/catalog.db")
        ns.push()

        @ray.remote
        class Recorder:
            def __init__(self):
                self._events: list[str] = []

            def record(self, label: str) -> None:
                self._events.append(label)

            def events(self) -> list[str]:
                return list(self._events)

        recorder = Recorder.remote()

        @ava.source(slug="produce")
        def produce(*, records=ns.records):
            ray.get(recorder.record.remote("produce_start"))
            result = records.append(
                pl.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
            )
            ray.get(recorder.record.remote("produce_end"))
            return result

        @ava.step(slug="consume")
        def consume(df: pl.DataFrame = ava.Stream(ns.records)):
            ray.get(recorder.record.remote("consume_start"))
            return sorted(df["id"].to_list())

        @ava.workflow
        def wf():
            return produce() >> consume()

        assert wf().run(executor=ava.RayExecutor()).result() == [1, 2, 3]

        # The consumer must not be submitted/started before the producer
        # completed: the scheduler gates it on parent completion, so the
        # closure-captured deferred ref can never block a worker slot.
        events = ray.get(recorder.events.remote())
        assert "produce_end" in events, events
        assert "consume_start" in events, events
        assert events.index("produce_end") < events.index("consume_start"), events
    finally:
        ray.shutdown()


@pytest.mark.ray
def test_ray_stream_deferred_upstream_is_a_scheduler_visible_dependency(tmp_path):
    """The deferred Stream parent must be a Ray-visible task dependency.

    Regression for a closure-capture bug: if the parent payload ref is hidden
    inside the ``stream_wrapper`` closure, Ray cannot see it as a dependency and
    may schedule the consumer before the producer finishes — the consumer then
    blocks (or under constrained resources deadlocks) inside worker-side
    resolution. The parent ref must be passed as an explicit top-level task
    argument so Ray waits for the producer before starting the consumer.

    With a free cluster (num_cpus=4) and a producer that blocks until released,
    the consumer's worker-side Stream resolution must NOT begin until after the
    producer completed:  produce_start -> produce_end -> consume_resolve_enter.
    """
    pytest.importorskip("ray")
    import threading
    import time

    import ray

    import avalanche.runtime.providers.stream as stream_mod

    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_cpus=4,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )

    orig_resolver = stream_mod._resolve_deferred_stream_upstream
    try:
        ns = _make_namespace(tmp_path, catalog_uri=f"sqlite:///{tmp_path}/catalog.db")
        ns.push()

        @ray.remote
        class Gate:
            def __init__(self):
                self._events: list[str] = []
                self._released = False

            def record(self, label: str) -> None:
                self._events.append(label)

            def release(self) -> None:
                self._released = True

            def released(self) -> bool:
                return self._released

            def events(self) -> list[str]:
                return list(self._events)

        gate = Gate.remote()

        @ava.source(slug="produce")
        def produce(*, records=ns.records):
            ray.get(gate.record.remote("produce_start"))
            while not ray.get(gate.released.remote()):
                time.sleep(0.05)
            result = records.append(pl.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}))
            ray.get(gate.record.remote("produce_end"))
            return result

        def _traced_resolver(*args, **kwargs):
            ray.get(gate.record.remote("consume_resolve_enter"))
            return orig_resolver(*args, **kwargs)

        stream_mod._resolve_deferred_stream_upstream = _traced_resolver

        @ava.step(slug="consume")
        def consume(df: pl.DataFrame = ava.Stream(ns.records)):
            return sorted(df["id"].to_list())

        @ava.workflow
        def wf():
            return produce() >> consume()

        def _releaser():
            time.sleep(2)
            ray.get(gate.release.remote())

        releaser = threading.Thread(target=_releaser, daemon=True)
        releaser.start()

        assert wf().run(executor=ava.RayExecutor()).result() == [1, 2, 3]
        releaser.join(timeout=5)

        labels = [label for label in ray.get(gate.events.remote())]
        assert "produce_end" in labels, labels
        assert "consume_resolve_enter" in labels, labels
        # The consumer's worker-side resolution must not begin before the
        # producer finished — proving Ray tracked the parent as a dependency.
        assert labels.index("produce_end") < labels.index("consume_resolve_enter"), labels
    finally:
        stream_mod._resolve_deferred_stream_upstream = orig_resolver
        ray.shutdown()


@pytest.mark.ray
def test_ray_multi_stream_same_consumer_uses_distinct_deferred_parents(tmp_path):
    """Two ``ava.Stream`` defaults on one consumer must not collide under Ray.

    The DAG lifts each deferred Stream parent into a distinct hidden top-level
    task kwarg (``_safe_hidden_kwarg(node_id, param_name, ...)``) and stamps the
    corresponding ``DeferredStreamUpstream`` per param. This regression proves,
    under real Ray, that:

    - each Stream param gets its own hidden parent kwarg (no shared name);
    - each param resolves against its own producer/table identity (no swap);
    - both consumers resolve as genuine passthrough (distinct table identities
      observed worker-side), so the assertion cannot be satisfied by a
      table-backed fallback that happens to read the same rows.

    Row counts alone are insufficient — a mismatched deferred parent could fall
    back to a table read and still return the right counts. The traced resolver
    records the deferred carriers' ``table_identity`` / ``parent_kwarg`` and the
    resolved passthrough identities so a bad match is caught.
    """
    pytest.importorskip("ray")
    import ray

    import avalanche.runtime.providers.stream as stream_mod

    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_cpus=4,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )

    orig_resolver = stream_mod._resolve_deferred_stream_upstream
    try:

        class MultiStreamNamespace(IcebergNs):
            ns_config = IcebergNsConfig(
                name="multi_stream_ns",
                base_location=str(tmp_path / "warehouse"),
            )
            left = IcebergTable(schema=RecordSchema)
            right = IcebergTable(schema=RecordSchema)

        ns = MultiStreamNamespace(
            catalog="multi-stream-catalog",
            load_catalog_props={"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db"},
        )
        ns.push()

        @ray.remote
        class Recorder:
            def __init__(self):
                self._events: list[dict[str, Any]] = []

            def record(self, event):
                self._events.append(event)

            def events(self):
                return list(self._events)

        recorder = Recorder.remote()

        def _traced_resolver(upstream_data, *args, **kwargs):
            from avalanche.types import DeferredStreamUpstream

            if isinstance(upstream_data, DeferredStreamUpstream):
                ray.get(
                    recorder.record.remote(
                        {
                            "table_identity": upstream_data.table_identity,
                            "parent_kwarg": upstream_data.parent_kwarg,
                        }
                    )
                )
            resolved = orig_resolver(upstream_data, *args, **kwargs)
            if resolved is not None:
                ray.get(
                    recorder.record.remote(
                        {
                            "resolved_table_identity": resolved.table_identity,
                            "height": resolved.to_polars().height,
                        }
                    )
                )
            return resolved

        stream_mod._resolve_deferred_stream_upstream = _traced_resolver

        @ava.source(slug="produce-left")
        def produce_left(*, table=ns.left):
            return table.append(
                pl.DataFrame({"id": [1, 2], "value": ["left-a", "left-b"]})
            )

        @ava.source(slug="produce-right")
        def produce_right(*, table=ns.right):
            return table.append(
                pl.DataFrame(
                    {"id": [10, 11, 12], "value": ["right-a", "right-b", "right-c"]}
                )
            )

        @ava.step(slug="consume-both")
        def consume_both(
            left_df: pl.DataFrame = ava.Stream(ns.left),
            right_df: pl.DataFrame = ava.Stream(ns.right),
        ):
            return {
                "left": sorted(left_df["id"].to_list()),
                "right": sorted(right_df["id"].to_list()),
            }

        @ava.workflow
        def wf():
            left_ref = produce_left()
            right_ref = produce_right()
            out = consume_both()
            (left_ref & right_ref) >> out
            return out

        out = wf().run(executor=ava.RayExecutor()).result()
        assert out == {"left": [1, 2], "right": [10, 11, 12]}, out

        events = ray.get(recorder.events.remote())

        # Both deferred carriers must reference their own table identity.
        table_identities = {
            e["table_identity"] for e in events if "table_identity" in e
        }
        assert table_identities == {ns.left.identifier, ns.right.identifier}, events

        # Distinct hidden parent kwargs — no collision between the two Streams.
        parent_kwargs = [e["parent_kwarg"] for e in events if "parent_kwarg" in e]
        assert len(parent_kwargs) == 2, events
        assert len(set(parent_kwargs)) == 2, events

        # Both resolved as genuine passthrough against distinct identities, so
        # neither silently fell back to a table read.
        resolved_identities = {
            e["resolved_table_identity"]
            for e in events
            if "resolved_table_identity" in e
        }
        assert resolved_identities == {ns.left.identifier, ns.right.identifier}, events
    finally:
        stream_mod._resolve_deferred_stream_upstream = orig_resolver
        ray.shutdown()


@pytest.mark.ray
def test_ray_plain_python_arg_receives_public_append_result(tmp_path):
    """A normal Python-arg consumer must receive a public AppendResult.

    The control/data split normalizes producer AppendResults into internal
    AppendResultHandles for transport, but a downstream node that consumes the
    upstream through an ordinary positional arg (not a Stream) must see the
    handle materialized back into a public AppendResult worker-side — never the
    internal transport type. Type facts are returned from the worker so the
    assertion runs on the driver (worker-side dict mutation would not).
    """
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_cpus=1,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )

    try:
        ns = _make_namespace(tmp_path, catalog_uri=f"sqlite:///{tmp_path}/catalog.db")
        ns.push()

        @ava.source(slug="produce")
        def produce(*, records=ns.records):
            return records.append(pl.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}))

        @ava.dest(slug="consume")
        def consume(result):
            from avalanche.types import AppendResult, AppendResultHandle

            return {
                "type": type(result).__name__,
                "is_append": isinstance(result, AppendResult),
                "is_handle": isinstance(result, AppendResultHandle),
                "height": result.to_polars().height,
            }

        @ava.workflow
        def wf():
            return produce() >> consume()

        out = wf().run(executor=ava.RayExecutor()).result()
        assert out["is_append"] is True, out
        assert out["is_handle"] is False, out
        assert out["height"] == 3, out
    finally:
        ray.shutdown()
