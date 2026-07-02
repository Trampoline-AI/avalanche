"""Avalanche Operator — workflow orchestration and execution."""

from .models import WorkflowDiscoveryDiagnostic
from .operator import Operator
from .registry import WorkflowRegistry, workflow_to_info

__all__ = [
    "Operator",
    "WorkflowDiscoveryDiagnostic",
    "WorkflowRegistry",
    "workflow_to_info",
]


def serve(workflow_paths: list[str], port: int = 7433, **kwargs) -> None:
    """Start the operator daemon with gRPC server."""
    from .server import serve as _serve

    op = Operator(workflow_paths, **kwargs)
    _serve(op, port=port, block=True)
