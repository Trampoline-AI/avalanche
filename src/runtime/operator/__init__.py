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
    web: bool = False,
    web_host: str = "127.0.0.1",
    web_port: int = 7435,
    web_trusted_proxy: bool = False,
    **kwargs,
) -> None:
    """Start the operator daemon with gRPC and optional browser listeners."""
    from .server import serve as _serve
    from .web import start_browser_server

    op = Operator(workflow_paths, webhook_port=webhook_port, **kwargs)
    browser_server = None
    try:
        if web:
            browser_server = start_browser_server(
                op,
                host=web_host,
                port=web_port,
                trust_non_loopback=web_trusted_proxy,
            )
            print(f"Avalanche web UI: {browser_server.endpoint}")
        _serve(op, port=port, block=True, host=host)
    finally:
        if browser_server is not None:
            browser_server.close()
        op.close()
