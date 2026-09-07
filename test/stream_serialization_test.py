"""Serialization contracts for table handles crossing process boundaries.

Stream steps ship their table handles to executor workers (Ray pickles the
function plus its defaults). Live catalog connections cannot travel; the
handle must carry the recipe to reconnect instead.
"""

from __future__ import annotations

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


@pytest.mark.ray
def test_ray_stream_deferred_upstream_is_a_scheduler_visible_dependency(tmp_path):
    """Consumer-side resolution must wait for its producer, even with spare CPUs."""
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
