"""Tests for Lance schema support that do not require the Lance runtime."""

import dataframely as dy
import pyarrow as pa

from avalanche.lance import LanceTable
from avalanche.lineage import ROW_LINEAGE_COLUMNS


class LanceSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    label = dy.String(nullable=False)


def test_lance_table_accepts_dataframely_schema_without_lance_import():
    table = LanceTable(schema=LanceSchema)

    assert isinstance(table.schema, pa.Schema)
    assert table.schema.names == ["id", "label", *ROW_LINEAGE_COLUMNS]
    assert table.schema_fields == ("id", "label", *ROW_LINEAGE_COLUMNS)


def test_lance_table_row_lineage_can_be_disabled_without_lance_import():
    table = LanceTable(schema=LanceSchema, row_lineage=False)

    assert isinstance(table.schema, pa.Schema)
    assert table.schema.names == ["id", "label"]
    assert table.schema_fields == ("id", "label")


def test_unbound_lance_table_version_does_not_require_lance_import():
    table = LanceTable(schema=LanceSchema)

    assert table.current_version_id is None
