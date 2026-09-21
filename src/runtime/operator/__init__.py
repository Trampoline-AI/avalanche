"""Avalanche Operator — workflow orchestration and execution."""

import signal
import sys
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


def _validate_listener_ports(port: int, webhook_port: int, web_port: int | None) -> None:
    ports = {"--port": port, "--webhook-port": webhook_port}
    if web_port is not None:
        ports["--web-port"] = web_port
    used: dict[int, str] = {}
    for name, value in ports.items():
        if type(value) is not int or not 0 <= value <= 65535:
            raise ValueError(f"{name} must be an integer from 0 to 65535")
        if value == 0:
            continue
        if value in used:
            raise ValueError(f"{used[value]} and {name} must differ")
        used[value] = name


def serve(
    workflow_paths: list[str],
    port: int = 7433,
    *,
    host: str = "127.0.0.1",
    webhook_port: int = 7434,
    web_port: int | None = None,
    **kwargs,
) -> None:
    """Serve gRPC, optionally with a loopback browser UI and JSON REST API."""
    from .server import _listen_address
    from .server import serve as _serve

    _validate_listener_ports(port, webhook_port, web_port)
    _listen_address(host, port)
    op = None
    server = None
    browser_server = None
    previous_handlers = {}
    cleaning_up = False

    def request_shutdown(_signum, _frame) -> None:
        if not cleaning_up:
            raise KeyboardInterrupt

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_shutdown)

        op = Operator(workflow_paths, webhook_port=webhook_port, **kwargs)
        _report_workflow_scan(op)
        server = _serve(op, port=port, block=False, host=host)
        bound_port = server._avalanche_bound_port
        print(f"  Operator ready: grpc://{_listen_address(host, bound_port)}")
        if web_port is not None:
            from .web import start_browser_server

            target_host = host.removeprefix("[").removesuffix("]")
            target_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(target_host, target_host)
            browser_server = start_browser_server(
                _listen_address(target_host, bound_port), host="127.0.0.1", port=web_port
            )
            print(f"  Web UI ready: {browser_server.endpoint}")
            print(f"  REST API ready: {browser_server.endpoint}/api/v1")
        print("Ready. Press Ctrl-C to stop.")
        while True:
            failure = op.wait_for_failure(timeout=0.1)
            if failure is not None:
                raise failure
    except KeyboardInterrupt:
        pass
    finally:
        cleaning_up = True
        primary_error = sys.exception()
        cleanup_error = None
        if browser_server is not None:
            try:
                browser_server.close()
            except Exception as exc:
                cleanup_error = exc
        if server is not None:
            try:
                server.stop(grace=1.0).wait(timeout=2.0)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if op is not None:
            try:
                op.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(f"Shutdown also failed: {cleanup_error}")
            else:
                raise cleanup_error
