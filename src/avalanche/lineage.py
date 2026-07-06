"""Row provenance helpers for Avalanche table writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
import pyarrow as pa

if TYPE_CHECKING:
    from .runtime import RunContext

ROW_LINEAGE_COLUMNS: tuple[str, ...] = (
    "_ava_updated_at",
    "_ava_execution_id",
    "_ava_workflow_name",
    "_ava_node_id",
    "_ava_node_name",
    "_ava_ctx_metadata",
)

ROW_LINEAGE_ARROW_FIELDS: tuple[pa.Field, ...] = (
    pa.field("_ava_updated_at", pa.timestamp("us")),
    pa.field("_ava_execution_id", pa.string()),
    pa.field("_ava_workflow_name", pa.string()),
    pa.field("_ava_node_id", pa.string()),
    pa.field("_ava_node_name", pa.string()),
    pa.field("_ava_ctx_metadata", pa.string()),
)


def validate_no_reserved_row_lineage_columns(field_names: tuple[str, ...]) -> None:
    """Reject user schemas that collide with framework-owned row lineage columns."""
    conflicts = sorted(set(field_names) & set(ROW_LINEAGE_COLUMNS))
    if conflicts:
        joined = ", ".join(conflicts)
        raise ValueError(
            "row_lineage=True reserves Avalanche provenance columns; "
            f"remove or rename: {joined}"
        )


def row_lineage_arrow_fields() -> tuple[pa.Field, ...]:
    """Return PyArrow fields for framework-owned row lineage columns."""
    return ROW_LINEAGE_ARROW_FIELDS


def add_row_lineage_to_arrow_schema(schema: pa.Schema) -> pa.Schema:
    """Return schema with Avalanche row lineage columns appended."""
    validate_no_reserved_row_lineage_columns(tuple(schema.names))
    return pa.schema([*schema, *ROW_LINEAGE_ARROW_FIELDS])


def _to_arrow(data: pl.DataFrame | pa.Table | pa.RecordBatch) -> pa.Table:
    if isinstance(data, pl.DataFrame):
        return data.to_arrow()
    if isinstance(data, pa.RecordBatch):
        return pa.Table.from_batches([data])
    return data


def _lineage_values(context: "RunContext | None") -> dict[str, Any]:
    updated_at = datetime.now(UTC).replace(tzinfo=None)
    metadata_json = None
    if context is not None and context.metadata:
        metadata_json = json.dumps(
            context.metadata,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        )

    return {
        "_ava_updated_at": updated_at,
        "_ava_execution_id": context.execution_id if context is not None else None,
        "_ava_workflow_name": context.workflow_name if context is not None else None,
        "_ava_node_id": context.node_id if context is not None else None,
        "_ava_node_name": context.node_name if context is not None else None,
        "_ava_ctx_metadata": metadata_json,
    }


def add_row_lineage_to_data(
    data: pl.DataFrame | pa.Table | pa.RecordBatch,
    *,
    context: "RunContext | None",
) -> pa.Table:
    """Append framework-owned row provenance columns to a batch of rows."""
    table = _to_arrow(data)
    existing_lineage_columns = [
        name for name in ROW_LINEAGE_COLUMNS if name in table.schema.names
    ]
    if existing_lineage_columns:
        table = table.drop(existing_lineage_columns)

    values = _lineage_values(context)
    arrays = [
        pa.array([values[field.name]] * table.num_rows, type=field.type)
        for field in ROW_LINEAGE_ARROW_FIELDS
    ]
    for field, array in zip(ROW_LINEAGE_ARROW_FIELDS, arrays):
        table = table.append_column(field, array)
    return table
