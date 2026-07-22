"""
Avalanche - Pythonic data workflows on Iceberg and Lance.

Provides a DAG-based framework for building data transformation workflows
with local and distributed execution.
"""

# DAG primitives
from .dag import Pipeline, Workflow, dest, pipeline, source, step, transform, workflow
from .execution_services import (
    EXECUTION_SERVICES_V1,
    ExecutionServiceReceipt,
    ExecutionServices,
    ExecutionServicesSpec,
    ExecutionTaskSpec,
)

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
from .input_ref import INPUT as input  # noqa: N811
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
from .run_handle import RunHandle

# Runtime primitives
from .runtime import (  # noqa: F401
    MAX_INLINE_FILE_BYTES,
    MAX_INLINE_REQUEST_BYTES,
    BaseContext,
    BaseInput,
    Cursor,
    File,
    Logger,
    ModelStream,
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

__version__ = "0.1.0rc1"


def __getattr__(name: str):
    if name == "agent":
        import avalanche.agent as value
    elif name in {
        "Agent",
        "InputField",
        "OutputField",
        "Signature",
        "agent_step",
    }:
        import avalanche.agent

        value = getattr(avalanche.agent, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value


__all__ = [
    # Decorators
    "source",
    "step",
    "transform",
    "dest",
    "workflow",
    "pipeline",
    "input",
    # Workflow
    "Workflow",
    "Pipeline",
    # Executors
    "Executor",
    "LocalExecutor",
    "RayExecutor",
    "get_default_executor",
    "EXECUTION_SERVICES_V1",
    "ExecutionServiceReceipt",
    "ExecutionServices",
    "ExecutionServicesSpec",
    "ExecutionTaskSpec",
    # Runtime
    "BaseContext",
    "BaseInput",
    "Cursor",
    "File",
    "MAX_INLINE_FILE_BYTES",
    "MAX_INLINE_REQUEST_BYTES",
    "Rerun",
    "RunContext",
    "RunHandle",
    "S3File",
    "Stream",
    "ModelStream",
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
