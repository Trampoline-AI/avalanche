"""
IcebergTable - Wrapper around PyIceberg Table.

Provides a convenient interface for working with Iceberg tables, including:
- Schema management (DataFramely support)
- Transparent proxying to underlying PyIceberg Table
- AppendResult for zero-copy data passing

All PyIceberg Table methods are accessible through this wrapper:
- append(df) - Append data to the table (returns AppendResult)
- scan() - Create a scan operation
- history() - View table history
- snapshots() - Access table snapshots
- metadata - Table metadata
- And all other PyIceberg Table methods
"""

from typing import TYPE_CHECKING, Any, Optional, Tuple, Union

import polars as pl
import pyarrow as pa
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.table import ALWAYS_TRUE, EMPTY_DICT, BooleanExpression, Properties, Table
from pyiceberg.types import NestedField, StringType, TimestampType

from ..lineage import ROW_LINEAGE_COLUMNS, add_row_lineage_to_data
from ..storage import NativeScanResult
from ..storage import Table as StorageTable
from ..types import AppendResult
from .schema import normalize_schema

if TYPE_CHECKING:
    from .namespace import IcebergAppendScan


def _with_row_lineage_schema(schema: IcebergSchema) -> IcebergSchema:
    existing = {field.name for field in schema.fields}
    conflicts = sorted(existing & set(ROW_LINEAGE_COLUMNS))
    if conflicts:
        joined = ", ".join(conflicts)
        raise ValueError(
            "row_lineage=True reserves Avalanche provenance columns; "
            f"remove or rename: {joined}"
        )

    next_field_id = max((field.field_id for field in schema.fields), default=0) + 1
    lineage_fields = [
        NestedField(
            field_id=next_field_id,
            name="_ava_updated_at",
            field_type=TimestampType(),
            required=False,
        ),
        NestedField(
            field_id=next_field_id + 1,
            name="_ava_run_id",
            field_type=StringType(),
            required=False,
        ),
        NestedField(
            field_id=next_field_id + 2,
            name="_ava_workflow_name",
            field_type=StringType(),
            required=False,
        ),
        NestedField(
            field_id=next_field_id + 3,
            name="_ava_node_id",
            field_type=StringType(),
            required=False,
        ),
        NestedField(
            field_id=next_field_id + 4,
            name="_ava_node_name",
            field_type=StringType(),
            required=False,
        ),
        NestedField(
            field_id=next_field_id + 5,
            name="_ava_ctx_metadata",
            field_type=StringType(),
            required=False,
        ),
    ]
    return IcebergSchema(*schema.fields, *lineage_fields)


class IcebergTable(StorageTable):
    """
    Wrapper around PyIceberg Table that proxies all Table methods.

    Supports DataFramely schemas and transparently proxies all PyIceberg Table methods
    via __getattr__. For type checking purposes, this class behaves like pyiceberg.table.Table.

    Type hint: IcebergTable behaves as a proxy to pyiceberg.table.Table and supports
    all its methods including append(), scan(), history(), snapshots(), metadata, etc.

    Usage:
        class MyNamespace(IcebergNs):
            ns_config = IcebergNsConfig(name="data", base_location="/path")

            documents = IcebergTable(schema=DocumentSchema)

        ns = MyNamespace(catalog="local", ...)
        ns.push()

        # All PyIceberg Table methods are transparently proxied
        ns.documents.append(df)  # PyArrow Table or DataFrame
        ns.documents.scan().to_arrow()
        ns.documents.history()
        ns.documents.snapshots()
        ns.documents.metadata
    """

    def __init__(self, schema: Any, *, row_lineage: bool = True):
        """
        Initialize table with a schema.

        Args:
            schema: DataFramely Schema class or PyIceberg Schema instance
        """
        super().__init__(row_lineage=row_lineage)

        # Convert schema if needed
        self.schema: IcebergSchema = normalize_schema(schema)
        if self.row_lineage:
            self.schema = _with_row_lineage_schema(self.schema)

        self._table: Table | None = None

    @property
    def identifier(self) -> str:
        """
        Get the full table identifier (namespace.table).

        Returns:
            Full identifier string
        """
        if self._ns is None:
            return self._table_name
        return f"{self._ns.name}.{self._table_name}"

    @property
    def location(self) -> str:
        """
        Get the table storage location.

        Returns:
            Storage path for this table
        """
        if self._ns is None:
            return ""
        return f"{self._ns.location}/{self._table_name}"

    @property
    def current_version_id(self) -> int | None:
        """Current Iceberg snapshot ID."""
        if self._table is None:
            return None

        snapshot = self._table.current_snapshot()
        if snapshot is None:
            return None
        return snapshot.snapshot_id

    def append(self, df: Union[pl.DataFrame, pa.Table, "pa.RecordBatch"]) -> AppendResult:
        """
        Append data to the table and return AppendResult.

        This wraps the underlying PyIceberg append() to return both the data
        and snapshot_id, enabling zero-copy data passing in workflows.

        Args:
            df: Data to append (Polars DataFrame or PyArrow Table/RecordBatch)

        Returns:
            AppendResult with data and snapshot_id

        Example:
            @ava.source
            def load_docs(*, documents=ns.documents):
                docs = fetch_from_s3()
                result = documents.append(docs.to_arrow())
                return result  # AppendResult for zero-copy passing
        """
        if self._table is None:
            raise AttributeError(
                "Cannot append - table has not been created yet. Call namespace.push() first."
            )

        # Convert to PyArrow if needed
        if isinstance(df, pl.DataFrame):
            arrow_data = df.to_arrow()
        else:
            arrow_data = df

        if self.row_lineage:
            from ..runtime import get_current_run_context

            arrow_data = add_row_lineage_to_data(
                arrow_data,
                context=get_current_run_context(),
            )

        arrow_data = self._cast_to_table_schema(arrow_data)

        # Call underlying PyIceberg append
        self._table.append(arrow_data)

        snapshot_id = self.current_version_id
        assert snapshot_id is not None
        return AppendResult(data=arrow_data, snapshot_id=snapshot_id)

    def _cast_to_table_schema(self, arrow_data: pa.Table | pa.RecordBatch) -> pa.Table:
        """Align Arrow field types/nullability with the declared Iceberg schema."""
        if isinstance(arrow_data, pa.RecordBatch):
            arrow_data = pa.Table.from_batches([arrow_data])

        return arrow_data.cast(self.schema.as_arrow())

    def scan(
        self,
        *args: Any,
        columns: list[str] | None = None,
        filter: Any = None,
        **kwargs: Any,
    ) -> NativeScanResult:
        """Create a neutral scan wrapper around the PyIceberg scan.

        Avalanche's shared table contract uses Lance-style scan names
        (`columns`, `filter`). PyIceberg expects `selected_fields`,
        `row_filter`, so Iceberg adapts those aliases here.
        """
        if self._table is None:
            raise AttributeError(
                "Cannot scan - table has not been created yet. Call namespace.push() first."
            )

        if columns is not None:
            if "selected_fields" in kwargs:
                raise TypeError("Pass either columns or selected_fields, not both")
            kwargs["selected_fields"] = tuple(columns)

        if filter is not None:
            if "row_filter" in kwargs:
                raise TypeError("Pass either filter or row_filter, not both")
            kwargs["row_filter"] = filter

        return NativeScanResult(self._table.scan(*args, **kwargs))

    def read(self) -> pl.DataFrame:
        """Read the current table contents as a Polars DataFrame."""
        return self.scan().to_polars()

    def merge(
        self,
        df: Any,
        *,
        on: list[str],
    ) -> None:
        """
        Merge (upsert) data into the table.

        Note: Not yet implemented. Will use PyIceberg's overwrite() with filter
        to achieve merge/upsert behavior.

        Args:
            df: DataFrame to merge (Polars or PyArrow)
            on: Key columns to match on

        Raises:
            NotImplementedError: This feature is not yet implemented
        """
        raise NotImplementedError(
            "merge() not yet implemented. "
            "Will be implemented using PyIceberg's overwrite() with dynamic filters. "
            "For now, use table.overwrite() or table.append() directly."
        )

    def append_scan(
        self,
        row_filter: Union[str, BooleanExpression] = ALWAYS_TRUE,
        selected_fields: Tuple[str, ...] = ("*",),
        case_sensitive: bool = True,
        start_snapshot_id: Optional[int] = None,
        snapshot_id: Optional[int] = None,
        options: Properties = EMPTY_DICT,
        limit: Optional[int] = None,
    ) -> "IcebergAppendScan":
        """
        Create an IcebergAppendScan for incremental processing.

        IcebergAppendScan filters files to return only data added since a start snapshot,
        enabling incremental data processing patterns.

        Args:
            row_filter: Filter expression for rows
            selected_fields: Columns to select
            case_sensitive: Whether filtering is case sensitive
            start_snapshot_id: Snapshot ID to use as baseline (only files added after this)
            snapshot_id: Snapshot ID to scan (defaults to current)
            options: Additional scan options
            limit: Maximum number of rows to return

        Returns:
            IcebergAppendScan instance for incremental scanning

        Example:
            # Get all new data since snapshot 123
            scan = table.append_scan(start_snapshot_id=123)
            df = scan.to_arrow()

            # Use with Stream for incremental processing
            stream = Stream(table)
            for batch in stream.read():
                process(batch)
        """
        # Import here to avoid circular dependency at module load time
        from .namespace import IcebergAppendScan

        return IcebergAppendScan.from_table(
            self,
            row_filter=row_filter,
            selected_fields=selected_fields,
            case_sensitive=case_sensitive,
            start_snapshot_id=start_snapshot_id,
            snapshot_id=snapshot_id,
            options=options,
            limit=limit,
        )

    def __getattr__(self, name: str) -> Any:
        """
        Proxy attribute access to underlying PyIceberg table.

        This allows transparent access to all PyIceberg Table methods like:
        - append(df)
        - scan()
        - history()
        - snapshots()
        - metadata
        - etc.

        Args:
            name: Attribute/method name

        Returns:
            Attribute from underlying table

        Raises:
            AttributeError: If attribute doesn't exist on underlying table
        """
        # Avoid infinite recursion for internal attributes
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        # Check if underlying table exists
        if self._table is None:
            raise AttributeError(
                f"Cannot access '{name}' - table has not been created yet. "
                f"Call namespace.push() first."
            )

        # Proxy to underlying table
        return getattr(self._table, name)

    def __repr__(self) -> str:
        return f"IcebergTable(table_name={self._table_name!r}, identifier={self.identifier!r})"
