"""Avalanche Operator — workflow orchestration and execution."""

import signal
import threading

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
    print(f"  Discovered {len(workflows)} workflow" f"{'' if len(workflows) == 1 else 's'}")


def serve(
    workflow_paths: list[str],
    port: int = 7433,
    *,
    host: str = "127.0.0.1",
    webhook_port: int = 7434,
    **kwargs,
) -> None:
    """Start the local operator daemon and fail on any lifecycle error."""
    from .server import serve as _serve

    op = Operator(workflow_paths, webhook_port=webhook_port, **kwargs)
    _report_workflow_scan(op)
    server = None
    stop_requested = threading.Event()
    previous_handlers = {}

    def request_shutdown(_signum, _frame) -> None:
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)

    try:
        server = _serve(op, port=port, block=False, host=host)
        print(f"  Operator ready: grpc://{host}:{port}")
        print("Ready. Press Ctrl-C to stop.")
        while not stop_requested.is_set():
            failure = op.wait_for_failure(timeout=0.1)
            if failure is not None:
                raise failure
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server is not None:
                server.stop(grace=1.0).wait(timeout=2.0)
        finally:
            op.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
