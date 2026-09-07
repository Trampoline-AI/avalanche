"""Real Stream reads and off-driver result materialization on both backends."""

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable


class RecordSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


@pytest.fixture(params=["iceberg", "lance"])
def namespace(request, tmp_path):
    if request.param == "iceberg":
        namespace_cls, config_cls, table_cls = IcebergNs, IcebergNsConfig, IcebergTable
        kwargs = {
            "catalog": "matrix",
            "load_catalog_props": {"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db"},
        }
    else:
        pytest.importorskip("lance")
        namespace_cls, config_cls, table_cls = LanceNamespace, LanceNamespaceConfig, LanceTable
        kwargs = {}

    class Matrix(namespace_cls):
        ns_config = config_cls(name="matrix", base_location=str(tmp_path / "warehouse"))
        left = table_cls(schema=RecordSchema, row_lineage=False)
        right = table_cls(schema=RecordSchema, row_lineage=False)

    ns = Matrix(**kwargs)
    ns.push()
    return ns


@pytest.fixture(params=["local", pytest.param("ray", marks=pytest.mark.ray)])
def executor(request):
    if request.param == "local":
        yield ava.LocalExecutor()
        return
    ray = pytest.importorskip("ray")
    ray.init(num_cpus=1, include_dashboard=False)
    try:
        yield ava.RayExecutor()
    finally:
        ray.shutdown()


def test_table_backed_stream_commits_progress(namespace, executor):
    table = namespace.left
    result = table.append(pl.DataFrame({"id": [1, 2], "value": ["a", "b"]}))

    @ava.step
    def consume(frame=ava.Stream(table, key="matrix", mode="append_scan")):
        return frame["value"].to_list()

    @ava.workflow
    def flow():
        return consume()

    assert flow().run(executor=executor).result(timeout=30) == ["a", "b"]
    assert ava.ProgressStore(table, key="matrix").get_cursor() == result.snapshot_id


def test_distinct_streams_and_plain_argument_materialize_without_deadlock(namespace, executor):
    left_table, right_table = namespace.left, namespace.right

    @ava.source
    def left():
        return left_table.append(pl.DataFrame({"id": [1, 2], "value": ["a", "b"]}))

    @ava.source
    def right():
        return right_table.append(pl.DataFrame({"id": [9], "value": ["z"]}))

    @ava.step
    def combine(left_frame=ava.Stream(left_table), right_frame=ava.Stream(right_table)):
        return left_frame["value"].to_list() + right_frame["value"].to_list()

    @ava.step
    def plain(result):
        assert isinstance(result, ava.AppendResult)
        return result.to_dicts()

    @ava.workflow
    def flow():
        left_ref, right_ref = left(), right()
        combined = combine()
        (left_ref & right_ref) >> combined
        return combined, plain(left_ref)

    # No lineage: a swapped/missing parent cannot pass by silently scanning its table.
    assert flow().run(executor=executor).result(timeout=30) == (
        ["a", "b", "z"],
        [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}],
    )
