"""Lance table backend for Avalanche."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException

from avalanche.lineage import add_row_lineage_to_arrow_schema, add_row_lineage_to_data
from avalanche.storage import ScanResult, Table
from avalanche.types import AppendResult

from .schema import normalize_schema


def _require_lance() -> Any:
    try:
        import lance
    except ImportError as exc:
        raise ImportError(
            "Lance table operations require the optional Lance dependency. "
            "Install it with `pip install avalanche-ai[lance]` or `uv sync --extra lance`."
        ) from exc
    return lance


def _to_arrow(df: pl.DataFrame | pa.Table | pa.RecordBatch) -> pa.Table:
    if isinstance(df, pl.DataFrame):
        return df.to_arrow()
    if isinstance(df, pa.RecordBatch):
        return pa.Table.from_batches([df])
    return df


class LanceScan(ScanResult):
    """Scan result for Lance tables."""

    def __init__(
        self,
        table: "LanceTable",
        *,
        columns: list[str] | None = None,
        filter: Any = None,
        limit: int | None = None,
    ):
        self._table = table
        self._columns = columns
        self._filter = filter
        self._limit = limit

    def to_arrow(self) -> pa.Table:
        return self._table._read_arrow(
            columns=self._columns,
            filter=self._filter,
            limit=self._limit,
        )


@dataclass(frozen=True)
class LanceHistoryEntry:
    """Backend-neutral history entry for a Lance data commit."""

    snapshot_id: int
    timestamp_ms: int


@dataclass(frozen=True)
class LanceSnapshot:
    """Minimal snapshot shape consumed by table-backed Stream."""

    snapshot_id: int
    parent_snapshot_id: int | None


def _fragment_file_paths(fragment_metadata: Any) -> tuple[str, ...]:
    return tuple(data_file.path for data_file in fragment_metadata.files)


def _is_data_operation(operation: Any) -> bool:
    operation_repr = repr(operation)
    return operation_repr.startswith("LanceOperation.Append") or operation_repr.startswith(
        "LanceOperation.Overwrite"
    )


class LanceAppendScan(ScanResult):
    """Scan rows introduced by one Lance data-producing version."""

    def __init__(
        self,
        table: "LanceTable",
        *,
        snapshot_id: int,
        columns: list[str] | None = None,
        filter: Any = None,
        limit: int | None = None,
    ):
        self._table = table
        self._snapshot_id = snapshot_id
        self._columns = columns
        self._filter = filter
        self._limit = limit

    def to_arrow(self) -> pa.Table:
        transaction = self._table._read_transaction(self._snapshot_id)
        if transaction is None:
            return self._empty_table()

        operation = transaction.operation
        if not _is_data_operation(operation):
            return self._empty_table()

        expected_file_paths = {
            _fragment_file_paths(fragment) for fragment in operation.fragments
        }
        if not expected_file_paths:
            return self._empty_table()

        version_dataset = self._table._dataset(version=self._snapshot_id)
        tables: list[pa.Table] = []
        remaining = self._limit

        for fragment in version_dataset.get_fragments():
            if _fragment_file_paths(fragment.metadata) not in expected_file_paths:
                continue

            fragment_limit = remaining if remaining is not None else None
            table = fragment.to_table(
                columns=self._columns,
                filter=self._filter,
                limit=fragment_limit,
            )
            tables.append(table)

            if remaining is not None:
                remaining -= table.num_rows
                if remaining <= 0:
                    break

        if not tables:
            return self._empty_table()
        return pa.concat_tables(tables)

    def _empty_table(self) -> pa.Table:
        empty = pa.Table.from_pylist([], schema=self._table.schema)
        return empty.select(self._columns) if self._columns is not None else empty


class LanceTransaction:
    """Metadata transaction adapter for Lance-backed tables."""

    def __init__(self, table: "LanceTable"):
        self._table = table
        self._dataset: Any | None = None
        self._updates: dict[str, str | None] = {}

    def __enter__(self) -> "LanceTransaction":
        if self._table._ns is None:
            raise AttributeError(
                "Cannot start transaction - table has not been bound to a namespace. "
                "Call namespace.push() first."
            )

        if self._table._dataset_exists():
            # Keep this handle through commit. If another transaction commits
            # first, Lance detects the stale read version and raises a conflict.
            self._dataset = self._table._dataset()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            return False

        if not self._updates:
            return False

        try:
            dataset = self._dataset or self._table._create_empty_dataset()
            dataset.update_metadata(self._updates)
        except OSError as exc:
            raise CommitFailedException(str(exc)) from exc

        return False

    def set_properties(self, **kwargs: Any) -> None:
        """Set metadata properties on commit."""
        self._updates.update({key: str(value) for key, value in kwargs.items()})

    def remove_properties(self, *keys: str) -> None:
        """Remove metadata properties on commit."""
        self._updates.update({key: None for key in keys})


def _reconnect_lance_table(
    location: str,
    schema: pa.Schema,
    row_lineage: bool,
    table_name: str,
) -> "LanceTable":
    """Rebuild a LanceTable handle after crossing a process boundary.

    Lance datasets are addressed by filesystem/object-store location, so the
    recipe is the location plus the declared schema.
    """
    table = LanceTable.__new__(LanceTable)
    table._ns = None
    table._table_name = table_name
    table.row_lineage = row_lineage
    table.schema = schema
    table._location_override = location
    return table


class LanceTable(Table):
    """Avalanche table backed by a Lance dataset."""

    def __init__(self, schema: Any, *, row_lineage: bool = True):
        super().__init__(row_lineage=row_lineage)
        self.schema: pa.Schema = normalize_schema(schema)
        if self.row_lineage:
            self.schema = add_row_lineage_to_arrow_schema(self.schema)
        self._location_override: str | None = None

    @property
    def location(self) -> str:
        if self._location_override is not None:
            return self._location_override
        return super().location

    def __reduce__(self) -> tuple[Any, tuple]:
        """Pickle as a reconnect recipe (dataset location + schema)."""
        location = self.location
        if not location:
            raise TypeError(
                "Cannot pickle LanceTable before namespace.push() binds it "
                "to a location"
            )
        return (
            _reconnect_lance_table,
            (location, self.schema, self.row_lineage, self._table_name),
        )

    @property
    def current_version_id(self) -> int | None:
        if not self._dataset_exists():
            return None

        dataset = self._dataset()
        version = getattr(dataset, "version", None)
        if callable(version):
            version = version()
        return int(version) if version is not None else None

    @property
    def properties(self) -> dict[str, str]:
        """Current Lance table metadata properties."""
        if not self._dataset_exists():
            return {}
        return dict(self._dataset().metadata)

    def append(self, df: pl.DataFrame | pa.Table | pa.RecordBatch) -> AppendResult:
        if not self.location:
            raise AttributeError(
                "Cannot append - table has not been bound to a namespace. "
                "Call namespace.push() first."
            )

        lance = _require_lance()
        arrow_data = _to_arrow(df)
        if self.row_lineage:
            from avalanche.runtime import get_current_run_context

            arrow_data = add_row_lineage_to_data(
                arrow_data,
                context=get_current_run_context(),
            )
        arrow_data = arrow_data.cast(self.schema)
        mode = "append" if self._dataset_exists() else "overwrite"
        lance.write_dataset(arrow_data, self.location, mode=mode)

        snapshot_id = self.current_version_id
        assert snapshot_id is not None
        return AppendResult(data=arrow_data, snapshot_id=snapshot_id)

    def snapshot_by_id(self, snapshot_id: int) -> LanceSnapshot | None:
        """Return minimal version metadata needed by table-backed streams."""
        transaction = self._read_transaction(snapshot_id)
        if transaction is None:
            return None

        parent_snapshot_id = int(transaction.read_version)
        return LanceSnapshot(
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent_snapshot_id if parent_snapshot_id > 0 else None,
        )

    def append_scan(
        self,
        *,
        start_snapshot_id: int | None = None,
        snapshot_id: int | None = None,
        columns: list[str] | None = None,
        filter: Any = None,
        selected_fields: tuple[str, ...] = ("*",),
        row_filter: Any = None,
        limit: int | None = None,
    ) -> LanceAppendScan:
        """Scan rows introduced by one Lance append/overwrite version.

        Lance only supports replaying the requested version from its direct
        parent. Calls that request a wider arbitrary range fail loudly instead
        of returning a misleading partial result.
        """
        if snapshot_id is None:
            snapshot_id = self.current_version_id
        if snapshot_id is None:
            raise ValueError("Cannot append_scan an empty Lance table")

        if start_snapshot_id is not None:
            snapshot = self.snapshot_by_id(snapshot_id)
            parent_snapshot_id = snapshot.parent_snapshot_id if snapshot else None
            if start_snapshot_id != parent_snapshot_id:
                raise NotImplementedError(
                    "LanceTable.append_scan only supports replaying one data "
                    "version from its direct parent; arbitrary snapshot ranges "
                    "are not supported"
                )

        if columns is None and selected_fields != ("*",):
            columns = list(selected_fields)
        if filter is None:
            filter = row_filter

        return LanceAppendScan(
            self,
            snapshot_id=snapshot_id,
            columns=columns,
            filter=filter,
            limit=limit,
        )

    def scan(
        self,
        *args: Any,
        columns: list[str] | None = None,
        filter: Any = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> LanceScan:
        if args or kwargs:
            raise TypeError("LanceTable.scan() supports columns, filter, and limit only")
        return LanceScan(
            self,
            columns=columns,
            filter=filter,
            limit=limit,
        )

    def read(self) -> pl.DataFrame:
        return self.scan().to_polars()

    def transaction(self) -> LanceTransaction:
        """Create a metadata transaction for cursor/progress CAS operations."""
        return LanceTransaction(self)

    def refresh(self) -> "LanceTable":
        """Refresh table metadata.

        Lance datasets are opened on demand, so this mirrors the Iceberg table
        API expected by ProgressStore without maintaining a local cache.
        """
        return self

    def history(self) -> list[LanceHistoryEntry]:
        """Return data-producing Lance versions for ProgressStore.

        Lance metadata updates create dataset versions too. Progress tracking
        should only iterate data commits; otherwise claim/done/cursor metadata
        commits become phantom pending snapshots.
        """
        if not self._dataset_exists():
            return []

        dataset = self._dataset()
        entries: list[LanceHistoryEntry] = []
        for version in dataset.versions():
            version_id = int(version["version"])
            transaction = dataset.read_transaction(version_id)
            if transaction is None:
                continue

            if not _is_data_operation(transaction.operation):
                continue

            version_metadata = version.get("metadata") or {}
            total_rows = int(version_metadata.get("total_rows", "0"))
            if total_rows == 0:
                continue

            entries.append(
                LanceHistoryEntry(
                    snapshot_id=version_id,
                    timestamp_ms=_timestamp_ms(version["timestamp"]),
                )
            )

        return entries

    def _dataset_path(self) -> Path:
        if not self.location:
            return Path("")
        return Path(self.location)

    def _dataset_exists(self) -> bool:
        if not self.location:
            return False

        dataset_path = self._dataset_path()
        return dataset_path.exists() and any(dataset_path.iterdir())

    def _dataset(self, version: int | None = None) -> Any:
        lance = _require_lance()
        return lance.dataset(self.location, version=version)

    def _read_transaction(self, version: int) -> Any | None:
        if not self._dataset_exists():
            return None
        return self._dataset().read_transaction(version)

    def _create_empty_dataset(self) -> Any:
        lance = _require_lance()
        empty = pa.Table.from_pylist([], schema=self.schema)
        return lance.write_dataset(empty, self.location, mode="overwrite")

    def _read_arrow(
        self,
        *,
        columns: list[str] | None = None,
        filter: Any = None,
        limit: int | None = None,
    ) -> pa.Table:
        if not self.location:
            raise AttributeError(
                "Cannot scan - table has not been bound to a namespace. "
                "Call namespace.push() first."
            )

        if not self._dataset_exists():
            empty = pa.Table.from_pylist([], schema=self.schema)
            if columns is not None:
                empty = empty.select(columns)
            return empty.slice(0, limit) if limit is not None else empty

        dataset = self._dataset()
        if hasattr(dataset, "to_table"):
            return dataset.to_table(
                columns=columns,
                filter=filter,
                limit=limit,
            )
        return dataset.scanner(columns=columns, filter=filter, limit=limit).to_table()

    def __repr__(self) -> str:
        return f"LanceTable(table_name={self._table_name!r}, identifier={self.identifier!r})"


def _timestamp_ms(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)
