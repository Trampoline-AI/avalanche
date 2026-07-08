"""
Type stubs for IcebergTable.

This file provides type hints indicating that IcebergTable proxies
all methods from pyiceberg.table.Table.
"""

from collections.abc import Sequence
from typing import Any, Optional, Tuple, Union

import polars as pl
import pyarrow as pa
from pydantic import BaseModel
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.table import BooleanExpression, Properties, Table

from avalanche.types import AppendResult

from .namespace import IcebergAppendScan, IcebergNamespace

class IcebergTable(Table):
    """
    IcebergTable proxies all Table methods via __getattr__.

    This stub indicates to type checkers that IcebergTable behaves like Table.
    """

    schema: IcebergSchema
    _table_name: str  # Simple table name (not full identifier)
    _ns: IcebergNamespace | None
    _table: Table | None
    row_model: type[BaseModel] | None

    def __init__(self, schema: Any, *, row_lineage: bool = ...) -> None: ...

    # Note: name() method is proxied from PyIceberg Table and returns identifier tuple
    @property
    def identifier(self) -> str: ...
    @property
    def location(self) -> str: ...
    @property
    def schema_fields(self) -> tuple[str, ...]: ...
    def scan(
        self,
        *args: Any,
        columns: list[str] | None = ...,
        filter: Any = ...,
        **kwargs: Any,
    ) -> Any: ...
    def append(
        self,
        df: pl.DataFrame | pa.Table | pa.RecordBatch | BaseModel | Sequence[BaseModel],
    ) -> AppendResult: ...
    def read_models(self) -> list[BaseModel]: ...
    def append_scan(
        self,
        row_filter: Union[str, BooleanExpression] = ...,
        selected_fields: Tuple[str, ...] = ...,
        case_sensitive: bool = ...,
        start_snapshot_id: Optional[int] = ...,
        snapshot_id: Optional[int] = ...,
        options: Properties = ...,
        limit: Optional[int] = ...,
    ) -> IcebergAppendScan: ...
    def __repr__(self) -> str: ...
