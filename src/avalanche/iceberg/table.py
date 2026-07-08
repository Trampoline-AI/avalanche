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

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Tuple, Union

import polars as pl
import pyarrow as pa
from pydantic import BaseModel
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

    next_field_id = schema.highest_field_id + 1
    lineage_fields = []
    for offset, name in enumerate(ROW_LINEAGE_COLUMNS):
        field_type = TimestampType() if name == "_ava_updated_at" else StringType()
        lineage_fields.append(
            NestedField(
                field_id=next_field_id + offset,
                name=name,
                field_type=field_type,
                required=False,
            )
        )
    return IcebergSchema(*schema.fields, *lineage_fields)


def _reconnect_table(
    catalog_name: str,
    catalog_props: dict,
    identifier: str,
    row_lineage: bool,
    row_model: type | None = None,
) -> "IcebergTable":
    """Rebuild a live IcebergTable handle after crossing a process boundary.

    Pickle carries the catalog address, not the connection; each worker
    opens its own catalog connection here at unpickle time.
    """
    from pyiceberg.catalog import load_catalog

    catalog = load_catalog(catalog_name, **catalog_props)
    table = IcebergTable.__new__(IcebergTable)
    table._ns = None
    table._table_name = identifier
    table.row_lineage = row_lineage
    table.row_model = row_model
    table._reconnect_spec = (
        catalog_name,
        catalog_props,
        identifier,
        row_lineage,
        row_model,
    )
    table._table = catalog.load_table(identifier)
    table.schema = table._table.schema()
    return table


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
        self.row_model = (
            schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None
        )

        # Convert schema if needed
        self.schema: IcebergSchema = normalize_schema(schema)
        if self.row_lineage:
            self.schema = _with_row_lineage_schema(self.schema)

        self._table: Table | None = None
        self._reconnect_spec: tuple[str, dict, str, bool, type | None] | None = None

    def __reduce__(self) -> tuple[Any, tuple]:
        """Pickle as a reconnect recipe (catalog address + identifier).

        Live catalog connections cannot cross process boundaries; executor
        workers rebuild their own connection via ``_reconnect_table``.
        """
        if self._reconnect_spec is not None:
            return (_reconnect_table, self._reconnect_spec)

        from .namespace import IcebergNamespace

        if self._table is None or not isinstance(self._ns, IcebergNamespace):
            raise TypeError(
                "Cannot pickle IcebergTable before namespace.push() binds it "
                "to a catalog"
            )

        catalog = self._ns.catalog
        properties = dict(catalog.properties)
        if ":memory:" in str(properties.get("uri", "")):
            raise TypeError(
                "Cannot pickle IcebergTable backed by an in-memory catalog; "
                "executor workers cannot reconnect to it. Use a file- or "
                "server-backed catalog."
            )

        return (
            _reconnect_table,
            (
                str(catalog.name),
                properties,
                self.identifier,
                self.row_lineage,
                self.row_model,
            ),
        )

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

    def append(
        self,
        df: Union[
            pl.DataFrame,
            pa.Table,
            "pa.RecordBatch",
            BaseModel,
            Sequence[BaseModel],
        ],
    ) -> AppendResult:
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
        df = self._coerce_append_input(df)
        if self._table is None:
            raise AttributeError(
                "Cannot append - table has not been created yet. Call namespace.push() first."
            )

        # Refresh so cross-process commits (e.g. Ray workers) are on the latest
        # metadata before this append; avoids committing from a stale snapshot.
        self._refresh_table_metadata()

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
        return AppendResult(
            data=arrow_data,
            snapshot_id=snapshot_id,
            table_identity=self.identifier,
            row_model=self.row_model,
        )

    def _cast_to_table_schema(self, arrow_data: pa.Table | pa.RecordBatch) -> pa.Table:
        """Align Arrow field types/nullability with the declared Iceberg schema."""
        if isinstance(arrow_data, pa.RecordBatch):
            arrow_data = pa.Table.from_batches([arrow_data])

        schema = self._table.schema() if self._table is not None else self.schema
        return arrow_data.cast(schema.as_arrow())

    def _refresh_table_metadata(self) -> None:
        """Reload catalog metadata so reads see commits from other processes.

        Distributed executors (e.g. Ray) commit appends from worker processes.
        The parent-process handle caches the table it loaded at push()/reconnect
        time and would otherwise scan a stale snapshot, returning rows written
        before the workflow ran. Refresh binds the latest committed metadata.
        """
        if self._table is None:
            return
        refreshed = self._table.refresh()
        if refreshed is not None:
            self._table = refreshed
        self.schema = self._table.schema()

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

        self._refresh_table_metadata()

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

            # For workflow-driven incremental processing, declare an append-scan
            # Stream provider on a task parameter instead of scanning manually:
            #   docs = ava.Stream(table, key="docs_to_chunks", mode="append_scan")
        """
        # Import here to avoid circular dependency at module load time
        from .namespace import IcebergAppendScan

        if self._table is None:
            raise AttributeError(
                "Cannot append_scan - table has not been created yet. "
                "Call namespace.push() first."
            )
        self._refresh_table_metadata()

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
