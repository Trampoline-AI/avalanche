"""Avalanche Operator — workflow orchestration and execution."""

from .models import (
    CatalogView,
    WorkflowDescriptor,
    WorkflowDiscoveryDiagnostic,
    WorkflowLocator,
)
from .operator import Operator
from .registry import (
    AmbiguousWorkflow,
    UnknownWorkflow,
    WorkflowRegistry,
    workflow_to_info,
)

__all__ = [
    "Operator",
    "AmbiguousWorkflow",
    "CatalogView",
    "UnknownWorkflow",
    "WorkflowDescriptor",
    "WorkflowDiscoveryDiagnostic",
    "WorkflowLocator",
    "WorkflowRegistry",
    "workflow_to_info",
]


def serve(
    workflow_paths: list[str],
    port: int = 7433,
    *,
    host: str = "127.0.0.1",
    webhook_port: int = 7434,
    **kwargs,
) -> None:
    """Start the operator daemon with gRPC server."""
    from .server import serve as _serve

    op = Operator(workflow_paths, webhook_port=webhook_port, **kwargs)
    try:
        _serve(op, port=port, block=True, host=host)
    finally:
        op.close()
