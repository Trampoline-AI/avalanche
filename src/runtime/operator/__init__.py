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


def _report_workflow_scan(operator: Operator) -> None:
    workflows = operator.get_catalog().workflows
    if not workflows:
        print("Workflow scan complete: 0 workflows loaded")
        return
    selectors = ", ".join(workflow.selector for workflow in workflows)
    print(f"Workflow scan complete: {len(workflows)} workflows loaded: {selectors}")


def serve(
    workflow_paths: list[str],
    port: int = 7433,
    *,
    host: str = "127.0.0.1",
    webhook_port: int = 7434,
    **kwargs,
) -> None:
    """Start the local operator daemon."""
    from .server import serve as _serve

    op = Operator(workflow_paths, webhook_port=webhook_port, **kwargs)
    _report_workflow_scan(op)
    try:
        _serve(op, port=port, block=True, host=host)
    finally:
        op.close()
