"""
Iceberg backend for Avalanche.

Provides IcebergNamespace and IcebergTable for managing Apache Iceberg tables.
Supports DataFramely schemas with automatic conversion to PyIceberg format.
"""

from .namespace import (
    IcebergAppendScan,
    IcebergNamespace,
    IcebergNs,
    IcebergNsConfig,
    IcebergTableGroup,
)
from .schema import dataframely_to_iceberg_schema, normalize_schema
from .table import IcebergTable

__all__ = [
    "IcebergNamespace",
    "IcebergNs",
    "IcebergNsConfig",
    "IcebergTable",
    "IcebergTableGroup",
    "IcebergAppendScan",
    # Schema utilities
    "dataframely_to_iceberg_schema",
    "normalize_schema",
]
