"""Backend-neutral storage contracts for Avalanche tables and namespaces."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from .types import AppendResult
from .utils import urljoin


class NamespaceConfig:
    """Configuration shared by table namespace backends."""

    def __init__(
        self,
        name: str,
        base_location: str,
        properties: dict[str, str] | None = None,
    ):
        self.name = name
        self.base_location = base_location
        self.properties = properties or {}


class ScanResult(ABC):
    """Minimal scan contract exposed across table backends."""

    @abstractmethod
    def to_arrow(self) -> pa.Table:
        """Return the scan result as a PyArrow table."""

    def to_polars(self) -> pl.DataFrame:
        """Return the scan result as a Polars DataFrame."""
        return pl.from_arrow(self.to_arrow())

    def to_arrow_batch_reader(self) -> pa.RecordBatchReader:
        """Return the scan result as a PyArrow batch reader."""
        return self.to_arrow().to_reader()


class NativeScanResult(ScanResult):
    """Adapter that adds the neutral scan contract to backend-native scans."""

    def __init__(self, native_scan: Any):
        self._native_scan = native_scan

    def to_arrow(self) -> pa.Table:
        return self._native_scan.to_arrow()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native_scan, name)


class Table(ABC):
    """Backend-neutral table contract."""

    schema: Any

    def __init__(self, *, row_lineage: bool = True) -> None:
        self._ns: Namespace | None = None
        self._table_name = ""
        self.row_lineage = row_lineage
        self.row_model: type[BaseModel] | None = None

    @property
    def identifier(self) -> str:
        """Full table identifier as ``namespace.table``."""
        if self._ns is None:
            return self._table_name
        return f"{self._ns.name}.{self._table_name}"

    @property
    def location(self) -> str:
        """Table storage location."""
        if self._ns is None:
            return ""
        return urljoin(self._ns.location, self._table_name)

    @property
    def schema_fields(self) -> tuple[str, ...]:
        """Field names in declaration order."""
        schema = self.schema

        if isinstance(schema, pa.Schema):
            return tuple(schema.names)

        fields = getattr(schema, "fields", None)
        if fields is not None:
            return tuple(field.name for field in fields)

        names = getattr(schema, "names", None)
        if names is not None:
            return tuple(names)

        raise TypeError(f"Cannot determine fields for schema type {type(schema)}")

    @property
    @abstractmethod
    def current_version_id(self) -> int | None:
        """Current table snapshot/version identity, if one exists."""

    @abstractmethod
    def append(
        self,
        df: pl.DataFrame | pa.Table | pa.RecordBatch | BaseModel | Sequence[BaseModel],
    ) -> AppendResult:
        """Append data and return the created version identity."""

    @abstractmethod
    def scan(self, *args: Any, **kwargs: Any) -> ScanResult:
        """Create a scan object for this table."""

    def read(self) -> pl.DataFrame:
        """Read the current table contents as a Polars DataFrame."""
        return self.scan().to_polars()

    def read_models(self) -> list[BaseModel]:
        """Read the current table contents as pydantic model instances."""
        if self.row_model is None:
            raise TypeError(
                "read_models() is unavailable because this table was not declared "
                "with a pydantic model schema."
            )

        from .model_frame import arrow_to_models

        data = self.scan().to_arrow()
        return arrow_to_models(data, self.row_model)

    def _coerce_append_input(
        self,
        df: pl.DataFrame | pa.Table | pa.RecordBatch | BaseModel | Sequence[BaseModel],
    ) -> pl.DataFrame | pa.Table | pa.RecordBatch:
        if isinstance(df, BaseModel):
            return self._models_to_arrow([df])

        if isinstance(df, (pl.DataFrame, pa.Table, pa.RecordBatch)):
            return df

        if isinstance(df, Sequence) and not isinstance(df, (str, bytes)):
            return self._models_to_arrow(df)

        return df

    def _models_to_arrow(self, models: Sequence[BaseModel]) -> pa.Table:
        if self.row_model is None:
            raise TypeError(
                "Cannot append pydantic model instances because this table was not "
                "declared with a pydantic model schema."
            )

        model_list = list(models)
        if not model_list:
            raise ValueError(
                "Cannot append zero rows; appending zero rows is rejected because it is "
                "meaningless for the backend and would make the returned AppendResult's "
                "cardinality ambiguous."
            )

        for item in model_list:
            if not isinstance(item, self.row_model):
                raise TypeError(
                    f"Expected instances of {self.row_model.__name__} when appending "
                    f"pydantic models; got {type(item).__name__}."
                )

        from .model_frame import models_to_arrow

        return models_to_arrow(model_list, self.row_model)


class TableGroup:
    """Group of related backend-neutral tables within a namespace."""

    def __init__(self, **tables: Table):
        self._ns: Namespace | None = None
        self._tables = tables

        for name, table in tables.items():
            setattr(self, name, table)

    def _get_all_tables(self) -> list[tuple[str, Table]]:
        return [(name, table) for name, table in self._tables.items()]


_provision_locks: dict[tuple[type["Namespace"], str, str], threading.Lock] = {}
_provision_locks_guard = threading.Lock()


class Namespace(ABC):
    """Backend-neutral namespace/catalog contract."""

    ns_config: NamespaceConfig | None = None

    def __init__(self) -> None:
        if not hasattr(self.__class__, "ns_config") or self.__class__.ns_config is None:
            raise ValueError(
                f"{self.__class__.__name__} must define ns_config. "
                "Example: ns_config = NamespaceConfig(name='...', base_location='...')"
            )

        config = self.__class__.ns_config
        self.name = config.name
        self.base_location = config.base_location

        for table_name, table in self._get_all_tables():
            table._ns = self
            table._table_name = table_name

    @property
    def location(self) -> str:
        """Namespace storage location."""
        return urljoin(str(self.base_location), self.name)

    def _provision(self) -> None:
        """Provision this namespace once at a time within the current process."""
        key = (type(self), str(self.base_location), self.name)
        with _provision_locks_guard:
            lock = _provision_locks.setdefault(key, threading.Lock())
        with lock:
            self.push()

    def _get_all_tables(self) -> list[tuple[str, Table]]:
        tables: list[tuple[str, Table]] = []

        # Walk class dictionaries instead of dir() so discovery follows
        # declaration order. This keeps backend-neutral namespace behavior stable
        # when plain tables and TableGroup declarations are mixed.
        seen: set[str] = set()
        for cls in reversed(self.__class__.mro()):
            if cls in (object, Namespace):
                continue

            for attr_name, attr_value in cls.__dict__.items():
                if attr_name.startswith("_"):
                    continue

                if isinstance(attr_value, Table):
                    if attr_name in seen:
                        continue
                    seen.add(attr_name)
                    tables.append((attr_name, attr_value))
                elif isinstance(attr_value, TableGroup):
                    for table_name, table in attr_value._get_all_tables():
                        if table_name in seen:
                            continue
                        seen.add(table_name)
                        tables.append((table_name, table))

        return tables

    @abstractmethod
    def push(self) -> None:
        """Create or update namespace and tables in the backend catalog."""

    @abstractmethod
    def drop(self, *, drop_tables: bool = False) -> None:
        """Drop this namespace from the backend catalog."""

    def list_tables(self) -> list[str]:
        """Declared table names in this namespace."""
        return [name for name, _ in self._get_all_tables()]
