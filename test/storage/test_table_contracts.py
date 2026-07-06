"""Backend-neutral table/catalog contract tests.

Every storage backend that claims to implement the Avalanche table contract must
pass this suite. Backend-specific tests can still cover native extras, but core
user-facing behavior belongs here so Lance and Iceberg do not drift.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import dataframely as dy
import polars as pl
import pyarrow as pa
import pytest

import avalanche as ava
from avalanche import Namespace, NamespaceConfig, Table, TableGroup
from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable
from avalanche.lineage import ROW_LINEAGE_COLUMNS

BUSINESS_COLUMNS = ("id", "name", "value")
TABLE_COLUMNS = (*BUSINESS_COLUMNS, *ROW_LINEAGE_COLUMNS)


class ContractSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    name = dy.String(nullable=False)
    value = dy.Int64(nullable=True)


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
            "catalog": "contract-catalog",
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
    class ContractNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"contract-{backend.name}",
            base_location=str(tmp_path),
        )
        records = backend.table_cls(schema=ContractSchema)
        grouped = TableGroup(extra=backend.table_cls(schema=ContractSchema))

    ns = ContractNamespace(**backend.namespace_kwargs(str(tmp_path)))
    ns.push()
    return ns


def _rows(*, start: int = 1, count: int = 3) -> pl.DataFrame:
    ids = list(range(start, start + count))
    return pl.DataFrame(
        {
            "id": ids,
            "name": [f"name-{i}" for i in ids],
            "value": [i * 10 for i in ids],
        }
    )


def _sorted_dicts(df: pl.DataFrame) -> list[dict[str, Any]]:
    return df.select(BUSINESS_COLUMNS).sort("id").to_dicts()


def _assert_default_lineage(df: pl.DataFrame, *, expected_rows: int) -> None:
    lineage = df.select(ROW_LINEAGE_COLUMNS)
    assert lineage.height == expected_rows
    for row in lineage.to_dicts():
        assert isinstance(row["_ava_updated_at"], datetime)
        assert row["_ava_execution_id"] is None
        assert row["_ava_workflow_name"] is None
        assert row["_ava_node_id"] is None
        assert row["_ava_node_name"] is None
        assert row["_ava_ctx_metadata"] is None


def test_backend_matrix_includes_all_declared_backends():
    assert [case.name for case in BACKENDS] == ["iceberg", "lance"]


def test_tables_accept_dataframely_schemas(backend: BackendCase):
    table = backend.table_cls(schema=ContractSchema)

    assert isinstance(table, Table)
    assert table.schema is not None
    assert table.schema_fields == TABLE_COLUMNS
    assert table.identifier == ""
    assert table.location == ""
    assert table.current_version_id is None


def test_row_lineage_can_be_disabled(backend: BackendCase):
    table = backend.table_cls(schema=ContractSchema, row_lineage=False)

    assert table.row_lineage is False
    assert table.schema_fields == BUSINESS_COLUMNS


def test_tables_reject_invalid_schemas(backend: BackendCase):
    with pytest.raises(TypeError, match="Schema must be either"):
        backend.table_cls(schema="not a schema")


def test_namespace_config_is_required(backend: BackendCase):
    class MissingConfigNamespace(backend.namespace_cls):
        records = backend.table_cls(schema=ContractSchema)

    with pytest.raises(ValueError, match="must define ns_config"):
        MissingConfigNamespace(**backend.namespace_kwargs("/tmp"))


def test_namespaces_discover_plain_and_grouped_tables(backend: BackendCase, tmp_path):
    class ContractNamespace(backend.namespace_cls):
        ns_config = backend.namespace_config_cls(
            name=f"contract-{backend.name}",
            base_location=str(tmp_path),
        )
        records = backend.table_cls(schema=ContractSchema)
        grouped = TableGroup(extra=backend.table_cls(schema=ContractSchema))

    ns = ContractNamespace(**backend.namespace_kwargs(str(tmp_path)))

    assert isinstance(ns, Namespace)
    assert isinstance(ns.ns_config, NamespaceConfig)
    assert ns.name == f"contract-{backend.name}"
    assert ns.base_location == str(tmp_path)
    assert ns.list_tables() == ["records", "extra"]

    assert ns.records._ns is ns
    assert ns.records._table_name == "records"
    assert ns.records.identifier == f"contract-{backend.name}.records"
    assert ns.records.location.endswith(f"contract-{backend.name}/records")

    grouped_extra = getattr(ns.grouped, "extra")
    assert grouped_extra._ns is ns
    assert grouped_extra._table_name == "extra"
    assert grouped_extra.identifier == f"contract-{backend.name}.extra"
    assert grouped_extra.location.endswith(f"contract-{backend.name}/extra")


def test_push_is_idempotent(namespace):
    namespace.push()
    namespace.push()

    assert namespace.records.current_version_id is None
    assert namespace.records.read().height == 0


def test_unbound_table_operations_raise_clear_errors(backend: BackendCase):
    table = backend.table_cls(schema=ContractSchema)

    with pytest.raises(AttributeError, match="namespace|created"):
        table.append(_rows())

    with pytest.raises(AttributeError, match="namespace|created"):
        table.scan().to_arrow()


def test_empty_bound_table_reads_as_empty_schema(namespace):
    table = namespace.records

    assert table.current_version_id is None
    assert table.scan().to_arrow().schema.names == list(TABLE_COLUMNS)
    assert table.scan().to_polars().columns == list(TABLE_COLUMNS)
    assert table.read().height == 0


def test_append_polars_returns_append_result_and_persists_rows(namespace):
    table = namespace.records
    rows = _rows()

    result = table.append(rows)

    assert result.snapshot_id == table.current_version_id
    assert result.snapshot_id is not None
    assert _sorted_dicts(result.to_polars()) == _sorted_dicts(rows)
    _assert_default_lineage(result.to_polars(), expected_rows=3)
    assert _sorted_dicts(table.scan().to_polars()) == _sorted_dicts(rows)
    assert _sorted_dicts(table.read()) == _sorted_dicts(rows)


def test_append_arrow_table_and_record_batch(namespace):
    table = namespace.records
    first = _rows(start=1, count=2)
    second = _rows(start=3, count=2)

    table.append(first.to_arrow())
    batch = second.to_arrow().to_batches()[0]
    assert isinstance(batch, pa.RecordBatch)
    table.append(batch)

    expected = pl.concat([first, second])
    assert _sorted_dicts(table.read()) == _sorted_dicts(expected)


def test_multiple_appends_advance_version_and_accumulate_rows(namespace):
    table = namespace.records

    first = table.append(_rows(start=1, count=2))
    second = table.append(_rows(start=3, count=2))

    assert first.snapshot_id is not None
    assert second.snapshot_id is not None
    assert second.snapshot_id != first.snapshot_id
    assert table.current_version_id == second.snapshot_id
    assert table.read().height == 4


def test_append_casts_to_declared_schema(namespace):
    table = namespace.records
    rows = pl.DataFrame(
        {
            "id": [1],
            "name": ["one"],
            # Int32 should be widened to declared Int64 where needed.
            "value": pl.Series("value", [10], dtype=pl.Int32),
        }
    )

    table.append(rows)
    arrow = table.scan().to_arrow()

    assert arrow.schema.field("value").type == pa.int64()
    assert _sorted_dicts(table.read()) == [{"id": 1, "name": "one", "value": 10}]


def test_scan_supports_projection_filter_and_limit(namespace):
    table = namespace.records
    table.append(_rows(start=1, count=5))

    projected = table.scan(columns=["id", "name"]).to_polars()
    assert projected.columns == ["id", "name"]
    assert projected.height == 5

    filtered = table.scan(filter="id = 3").to_polars()
    assert _sorted_dicts(filtered) == [{"id": 3, "name": "name-3", "value": 30}]

    combined = table.scan(filter="id > 2", columns=["name"], limit=2).to_polars()
    assert combined.columns == ["name"]
    assert combined.height == 2
    assert combined["name"].to_list() == ["name-3", "name-4"]


def test_row_lineage_captures_workflow_context(namespace):
    table = namespace.records

    @ava.source
    def load_rows(*, records=table):
        return records.append(_rows(count=1))

    @ava.workflow
    def lineage_flow():
        return load_rows()

    result = lineage_flow().run(
        executor=ava.LocalExecutor(),
        execution_id="exec_123",
        context={"metadata": {"attempt": 2, "tenant": "acme"}},
    )
    row = result.to_polars().to_dicts()[0]

    assert isinstance(row["_ava_updated_at"], datetime)
    assert row["_ava_execution_id"] == "exec_123"
    assert row["_ava_workflow_name"] == "lineage_flow"
    assert row["_ava_node_id"] == "load_rows_1"
    assert row["_ava_node_name"] == "load_rows"
    assert json.loads(row["_ava_ctx_metadata"]) == {"attempt": 2, "tenant": "acme"}


def test_drop_tables_removes_backend_data(namespace):
    table = namespace.records
    table.append(_rows())
    assert table.read().height == 3

    namespace.drop(drop_tables=True)

    assert table.current_version_id is None

    namespace.push()
    assert table.current_version_id is None
    assert table.read().height == 0
