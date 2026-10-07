"""
Avalanche - Pythonic data workflows on Iceberg and Lance.

Provides a DAG-based framework for building data transformation workflows
with local and distributed execution.

The Iceberg and Lance storage backends, the agent module, and the classifier
module are imported lazily on first attribute access, so ``import avalanche``
does not load the catalog and columnar stacks (pyiceberg, pyarrow, pandas,
SQLAlchemy, object-store clients) for programs that only use the DAG,
executor, and runtime API.
"""

from importlib import import_module

# DAG primitives
# Execution engines
from runtime.executor import Executor, LocalExecutor, RayExecutor, get_default_executor

from .dag import Pipeline, Workflow, dest, pipeline, source, step, transform, workflow
from .execution_services import (
    EXECUTION_SERVICES_V1,
    ExecutionServiceReceipt,
    ExecutionServices,
    ExecutionServicesSpec,
    ExecutionTaskSpec,
)
from .input_ref import INPUT as input  # noqa: N811
from .model_frame import Json

# Progress tracking
from .progress import ProgressStore
from .run_handle import RunHandle

# Runtime primitives
from .runtime import (  # noqa: F401
    BaseContext,
    BaseInput,
    Cursor,
    File,
    Logger,
    ModelStream,
    Rerun,
    RunContext,
    Stream,
    Workspace,
    consume_stream,
)

# Storage contracts
from .storage import Namespace, NamespaceConfig, ScanResult, Table, TableGroup

# Types
from .types import AppendResult, SnapshotMetadata, SnapshotState
from .webhook import Webhook

__version__ = "0.10.0"

# Submodules and names resolved lazily on first attribute access (see __getattr__).
_LAZY_SUBMODULES = frozenset({"agent", "classifier", "iceberg", "lance"})
_LAZY_EXPORTS: dict[str, str] = {
    # Agent
    "Agent": "agent",
    "InputField": "agent",
    "OutputField": "agent",
    "Signature": "agent",
    "agent_step": "agent",
    # Classifier
    "Classifier": "classifier",
    "ClassificationResult": "classifier",
    "ClassifierStepError": "classifier",
    "ClassifierStepExecutionError": "classifier",
    "classifier_step": "classifier",
    # Evaluations
    "EvalContext": "evaluations",
    "Metric": "evaluations",
    "Evaluations": "evaluations",
    # Iceberg backend
    "IcebergAppendScan": "iceberg",
    "IcebergNamespace": "iceberg",
    "IcebergNs": "iceberg",
    "IcebergNsConfig": "iceberg",
    "IcebergTable": "iceberg",
    "IcebergTableGroup": "iceberg",
    # Lance backend
    "LanceNamespace": "lance",
    "LanceNamespaceConfig": "lance",
    "LanceNs": "lance",
    "LanceNsConfig": "lance",
    "LanceTable": "lance",
}


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        value = import_module(f"{__name__}.{name}")
    elif name in _LAZY_EXPORTS:
        value = getattr(import_module(f"{__name__}.{_LAZY_EXPORTS[name]}"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _LAZY_SUBMODULES)


__all__ = [
    # Decorators
    "source",
    "step",
    "transform",
    "dest",
    "workflow",
    "pipeline",
    "classifier_step",
    "input",
    # Workflow
    "Workflow",
    "Pipeline",
    "Webhook",
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
    "Workspace",
    "Rerun",
    "RunContext",
    "RunHandle",
    "Stream",
    "ModelStream",
    "Logger",
    "consume_stream",
    # Progress
    "ProgressStore",
    # Types
    "AppendResult",
    "Classifier",
    "ClassificationResult",
    "ClassifierStepError",
    "ClassifierStepExecutionError",
    "EvalContext",
    "Metric",
    "Evaluations",
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
