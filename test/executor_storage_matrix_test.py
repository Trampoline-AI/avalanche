"""Executor/storage matrix contracts for workflow Stream paths."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import dataframely as dy
import polars as pl
import pytest

import avalanche as ava
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable


class MatrixSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


@dataclass(frozen=True)
class BackendCase:
    name: str
    table_cls: type
    namespace_cls: type
    namespace_config_cls: type
    namespace_kwargs: dict[str, Any]
    requires: tuple[str, ...] = ()


def _backend_cases(tmp_path) -> list[BackendCase]:
    return [
        BackendCase(
            name="iceberg",
            table_cls=IcebergTable,
            namespace_cls=IcebergNs,
            namespace_config_cls=IcebergNsConfig,
            namespace_kwargs={
                "catalog": "matrix-catalog",
                "load_catalog_props": {
                    "type": "sql",
                    "uri": f"sqlite:///{tmp_path}/catalog.db",
                },
            },
        ),
        BackendCase(
            name="lance",
            table_cls=LanceTable,
            namespace_cls=LanceNamespace,
            namespace_config_cls=LanceNamespaceConfig,
            namespace_kwargs={},
            requires=("lance",),
        ),
    ]


@pytest.fixture(params=["iceberg", "lance"])
def backend(request: pytest.FixtureRequest, tmp_path) -> BackendCase:
    cases = {case.name: case for case in _backend_cases(tmp_path)}
    case = cases[request.param]
    for module_name in case.requires:
        pytest.importorskip(module_name)
    return case


@pytest.fixture(params=["local", "ray"])
def executor(request: pytest.FixtureRequest) -> Iterator[ava.Executor]:
    if request.param == "local":
        yield ava.LocalExecutor()
        return

    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=4,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )
    try:
        yield ava.RayExecutor()
    finally:
        ray.shutdown()


@pytest.fixture
def matrix_namespace(backend: BackendCase, tmp_path):
    class MatrixNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"matrix-{backend.name}",
            base_location=str(tmp_path / "warehouse"),
        )
        left = backend.table_cls(schema=MatrixSchema)
        right = backend.table_cls(schema=MatrixSchema)
        dest = backend.table_cls(schema=MatrixSchema)

    ns = MatrixNamespace(**backend.namespace_kwargs)
    ns.push()
    return ns


def test_table_backed_stream_runs_on_executor_storage_matrix(matrix_namespace, executor):
    ns = matrix_namespace
    result = ns.left.append(pl.DataFrame({"id": [1, 2], "value": ["a", "b"]}))

    @ava.step(slug="consume-table")
    def consume_table(
        df: pl.DataFrame = ava.Stream(ns.left, key="matrix_table", mode="append_scan"),
    ):
        return sorted(df["id"].to_list())

    @ava.workflow
    def wf():
        return consume_table()

    assert wf().run(executor=executor) == [1, 2]
    assert ava.ProgressStore(ns.left, key="matrix_table").get_cursor() == result.snapshot_id


def test_passthrough_multistream_runs_on_executor_storage_matrix(
    matrix_namespace, executor
):
    ns = matrix_namespace

    @ava.source(slug="produce-left")
    def produce_left(*, table=ns.left):
        return table.append(pl.DataFrame({"id": [1, 2], "value": ["left-a", "left-b"]}))

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

    assert wf().run(executor=executor) == {
        "left": [1, 2],
        "right": [10, 11, 12],
    }
