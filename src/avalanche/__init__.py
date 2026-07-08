"""
Avalanche - Pythonic data workflows on Iceberg and Lance.

Provides a DAG-based framework for building data transformation workflows
with local and distributed execution.
"""

# DAG primitives
from .dag import Pipeline, Workflow, dest, pipeline, source, step, transform, workflow

# Execution engines
from .executor import Executor, LocalExecutor, RayExecutor, get_default_executor

# Iceberg backend
from .iceberg import (
    IcebergAppendScan,
    IcebergNamespace,
    IcebergNs,
    IcebergNsConfig,
    IcebergTable,
    IcebergTableGroup,
)
from .lance import (
    LanceNamespace,
    LanceNamespaceConfig,
    LanceNs,
    LanceNsConfig,
    LanceTable,
)
from .model_frame import Json

# Progress tracking
from .progress import ProgressStore

# Runtime primitives
from .runtime import (  # noqa: F401
    MAX_INLINE_FILE_BYTES,
    MAX_INLINE_REQUEST_BYTES,
    BaseContext,
    BaseInput,
    Cursor,
    File,
    Logger,
    Rerun,
    RunContext,
    S3File,
    Stream,
    consume_stream,
)

# Storage contracts
from .storage import Namespace, NamespaceConfig, ScanResult, Table, TableGroup

# Types
from .types import AppendResult, SnapshotMetadata, SnapshotState

__version__ = "0.1.0rc0"


__all__ = [
    # Decorators
    "source",
    "step",
    "transform",
    "dest",
    "workflow",
    "pipeline",
    # Workflow
    "Workflow",
    "Pipeline",
    # Executors
    "Executor",
    "LocalExecutor",
    "RayExecutor",
    "get_default_executor",
    # Runtime
    "BaseContext",
    "BaseInput",
    "Cursor",
    "File",
    "MAX_INLINE_FILE_BYTES",
    "MAX_INLINE_REQUEST_BYTES",
    "Rerun",
    "RunContext",
    "S3File",
    "Stream",
    "Logger",
    "consume_stream",
    # Progress
    "ProgressStore",
    # Types
    "AppendResult",
    "SnapshotState",
    "SnapshotMetadata",
    "Json",
    # Storage contracts
    "Namespace",
    "NamespaceConfig",
    "Table",
    "TableGroup",
    "ScanResult",
    # Iceberg
    "IcebergNamespace",
    "IcebergNs",
    "IcebergNsConfig",
    "IcebergTable",
    "IcebergTableGroup",
    "IcebergAppendScan",
    # Lance
    "LanceNamespace",
    "LanceNs",
    "LanceNamespaceConfig",
    "LanceNsConfig",
    "LanceTable",
]
