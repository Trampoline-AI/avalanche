"""gRPC server wrapping the Operator."""

from __future__ import annotations

import ipaddress
import json
import logging
import signal
import threading
from concurrent import futures
from typing import Any

import grpc

from ._grpc import _BOUNDED_MESSAGE_OPTIONS
from .operator import Operator
from .proto import operator_pb2_grpc as pb_grpc
from .registry import AmbiguousWorkflow

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7433
DEFAULT_HOST = "127.0.0.1"
TRACE_CHUNK_BYTES = 1024 * 1024


def serve(
    operator: Operator,
    port: int = DEFAULT_PORT,
    block: bool = True,
    *,
    host: str = DEFAULT_HOST,
) -> grpc.Server:
    """Start the gRPC server.

    Args:
        operator: The Operator instance to serve.
        port: Port to listen on.
        block: If True, blocks until server is terminated.
        host: Explicit listen host. The loopback default limits reachability
            but does not authenticate callers. A non-loopback host requires an
            external trusted and authenticated boundary.

    Returns:
        The gRPC server (useful for testing when block=False).
    """
    server: grpc.Server | None = None
    try:
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            options=_BOUNDED_MESSAGE_OPTIONS,
        )
        # Imported here to keep the module dependency one-directional:
        # server_v2 reuses helpers from this module.
        from .server_v2 import OperatorV2Servicer

        pb_grpc.add_OperatorServiceV2Servicer_to_server(OperatorV2Servicer(operator), server)
        listen_address = _listen_address(host, port)
        if not _is_loopback_host(host):
            logger.warning(
                "Operator gRPC is listening on non-loopback address %s without built-in "
                "authentication; use only behind a trusted and authenticated boundary",
                listen_address,
            )
        if server.add_insecure_port(listen_address) == 0:
            raise RuntimeError(f"Could not bind operator gRPC server to {listen_address}")
        server.start()
    except BaseException:
        try:
            if server is not None:
                server.stop(grace=0)
        finally:
            operator.close()
        raise
    logger.info("Operator gRPC server listening on %s", listen_address)

    if block:
        previous_handlers: dict[int, Any] = {}
        monitor_stop = threading.Event()
        fatal_error: list[Exception] = []
        failure_monitor: threading.Thread | None = None

        if isinstance(operator, Operator):

            def stop_for_fatal_operator_error() -> None:
                while not monitor_stop.is_set():
                    failure = operator.wait_for_failure(timeout=0.1)
                    if failure is None:
                        continue
                    fatal_error.append(failure)
                    server.stop(grace=0)
                    return

            failure_monitor = threading.Thread(
                target=stop_for_fatal_operator_error,
                name="avalanche-operator-failure-monitor",
                daemon=True,
            )
            failure_monitor.start()

        def request_shutdown(signum, _frame) -> None:
            logger.info("Received signal %s; shutting down", signum)
            server.stop(grace=1.0)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_shutdown)
        try:
            server.wait_for_termination()
        except KeyboardInterrupt:
            request_shutdown(signal.SIGINT, None)
        finally:
            monitor_stop.set()
            if failure_monitor is not None:
                failure_monitor.join(timeout=1.0)
            try:
                server.stop(grace=1.0).wait(timeout=2.0)
            finally:
                operator.close()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
        if fatal_error:
            raise fatal_error[0]

    return server


def _listen_address(host: str, port: int) -> str:
    if type(host) is not str or not host:
        raise ValueError("Operator listen host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Operator listen port must be an integer from 0 to 65535")
    normalized = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    display_host = f"[{normalized}]" if ":" in normalized else normalized
    return f"{display_host}:{port}"


def _is_loopback_host(host: str) -> bool:
    normalized = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _decode_json_object(payload: str, field_name: str) -> dict[str, Any] | None:
    if not payload:
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {field_name}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _set_input_field(payload: dict[str, Any], field_name: str, value: Any) -> None:
    if field_name in payload:
        raise ValueError(f"Duplicate input field '{field_name}'")
    payload[field_name] = value


def _ambiguous_detail(exc: AmbiguousWorkflow) -> str:
    candidates = "\n".join(f"  {item}" for item in sorted(exc.candidate_ids))
    return f"{exc.selector!r} is ambiguous:\n{candidates}"
