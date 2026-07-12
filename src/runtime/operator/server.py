"""gRPC server wrapping the Operator."""

from __future__ import annotations

import json
import logging
import signal
import threading
from concurrent import futures
from typing import Any

import grpc

from avalanche.runtime import MAX_INLINE_FILE_BYTES, MAX_INLINE_REQUEST_BYTES

from .convert import (
    discovery_diagnostic_to_proto,
    run_state_to_proto,
    workflow_info_to_proto,
)
from .operator import Operator
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc
from .registry import AmbiguousWorkflow, UnknownWorkflow

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7433


class OperatorServicer(pb_grpc.OperatorServiceServicer):
    """gRPC servicer that delegates to an Operator instance."""

    def __init__(self, operator: Operator) -> None:
        self._op = operator

    def ListFlows(self, request, context):  # noqa: N802
        workflows = self._op.list_workflows()
        return pb.FlowList(
            flows=[workflow_info_to_proto(p) for p in workflows],
            diagnostics=[
                discovery_diagnostic_to_proto(item)
                for item in self._op.list_diagnostics()
            ],
        )

    def StartRun(self, request, context):  # noqa: N802
        try:
            run_input = _decode_input_payload(request)
            run_context = _decode_json_object(request.context_json, "context_json")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        selector = request.workflow_selector or request.flow_name
        try:
            run_id = self._op.start_run(
                selector,
                input=run_input,
                context=run_context,
            )
            return pb.StartRunResponse(run_id=run_id)
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except UnknownWorkflow as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, exc.args[0])
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

    def CancelRun(self, request, context):  # noqa: N802
        self._op.cancel_run(request.run_id)
        return pb.Empty()

    def ListRuns(self, request, context):  # noqa: N802
        selector = request.workflow_selector or request.flow_name
        try:
            runs = self._op.list_runs(selector)
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        return pb.RunList(runs=[run_state_to_proto(r) for r in runs])

    def GetRun(self, request, context):  # noqa: N802
        run = self._op.get_run(request.run_id)
        if run is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return run_state_to_proto(run)

    def StreamUpdates(self, request, context):  # noqa: N802
        """Server-streaming RPC: yields RunUpdate messages as state changes."""
        q = self._op.subscribe()
        try:
            while context.is_active():
                try:
                    seq, run = q.get(timeout=1.0)
                    if seq <= request.since_sequence:
                        continue
                    yield pb.RunUpdate(
                        sequence=seq,
                        run=run_state_to_proto(run),
                    )
                except Exception:
                    # Queue.get timeout — just loop and check context.is_active()
                    continue
        finally:
            self._op.unsubscribe(q)


def serve(operator: Operator, port: int = DEFAULT_PORT, block: bool = True) -> grpc.Server:
    """Start the gRPC server.

    Args:
        operator: The Operator instance to serve.
        port: Port to listen on.
        block: If True, blocks until server is terminated.

    Returns:
        The gRPC server (useful for testing when block=False).
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb_grpc.add_OperatorServiceServicer_to_server(
        OperatorServicer(operator), server
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"Operator gRPC server listening on port {port}")

    if block:
        previous_handlers: dict[int, Any] = {}
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
            try:
                server.stop(grace=1.0).wait(timeout=2.0)
            finally:
                operator.close()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)

    return server


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


def _decode_input_payload(request) -> dict[str, Any] | None:
    payload = _decode_json_object(request.input_json, "input_json") or {}
    _validate_inline_request_size(request.input_files)
    for file in request.input_files:
        if not file.field_name:
            raise ValueError("input file attachment is missing field_name")
        if len(file.content) > MAX_INLINE_FILE_BYTES:
            raise ValueError(
                f"File attachment '{file.field_name}' exceeds the maximum inline file size "
                f"of {MAX_INLINE_FILE_BYTES} bytes. Use ava.S3File for larger files."
            )
        _set_input_field(payload, file.field_name, {
            "name": file.name or None,
            "content": bytes(file.content),
            "content_type": file.content_type or None,
            "sha256": file.sha256 or None,
        })
    for file in request.input_s3_files:
        if not file.field_name:
            raise ValueError("input S3 file reference is missing field_name")
        _set_input_field(payload, file.field_name, {
            "uri": file.uri,
            "version_id": file.version_id or None,
            "etag": file.etag or None,
            "size_bytes": file.size_bytes or None,
            "content_type": file.content_type or None,
        })
    return payload or None


def _validate_inline_request_size(files) -> None:
    total = sum(len(file.content) for file in files)
    if total > MAX_INLINE_REQUEST_BYTES:
        raise ValueError(
            f"Inline file attachments total {total} bytes, exceeding the maximum "
            f"inline request size of {MAX_INLINE_REQUEST_BYTES} bytes. "
            "Use ava.S3File for larger files."
        )


def _set_input_field(payload: dict[str, Any], field_name: str, value: Any) -> None:
    if field_name in payload:
        raise ValueError(f"Duplicate input field '{field_name}'")
    payload[field_name] = value


def _ambiguous_detail(exc: AmbiguousWorkflow) -> str:
    candidates = "\n".join(f"  {item}" for item in sorted(exc.candidate_ids))
    return f"{exc.selector!r} is ambiguous:\n{candidates}"
