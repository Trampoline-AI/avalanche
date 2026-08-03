"""gRPC server wrapping the Operator."""

from __future__ import annotations

import ipaddress
import json
import logging
import queue
import signal
import threading
from concurrent import futures
from typing import Any

import grpc

from avalanche.runtime import File

from ._grpc import _BOUNDED_MESSAGE_OPTIONS
from .convert import (
    agent_event_descriptor_to_proto,
    catalog_snapshot_to_proto,
    log_record_descriptor_to_proto,
    operator_update_envelope_to_proto,
    run_snapshot_to_proto,
    run_summary_to_proto,
)
from .operator import (
    InvalidRunIdError,
    Operator,
    RunAlreadyExistsError,
    RunResultNotReadyError,
    RunResultUnavailableError,
    StructuralBaselineUnavailableError,
)
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc
from .registry import AmbiguousWorkflow, UnknownWorkflow

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7433
DEFAULT_HOST = "127.0.0.1"
TRACE_CHUNK_BYTES = 1024 * 1024


class OperatorServicer(pb_grpc.OperatorServiceServicer):
    """gRPC servicer that delegates to an Operator instance."""

    def __init__(self, operator: Operator) -> None:
        self._op = operator

    def GetCatalog(self, request, context):  # noqa: N802
        return catalog_snapshot_to_proto(self._op.get_catalog())

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
                run_id=request.run_id or None,
                input=run_input,
                context=run_context,
            )
            return pb.StartRunResponse(run_id=run_id)
        except InvalidRunIdError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except RunAlreadyExistsError as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except UnknownWorkflow as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, exc.args[0])
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

    def CancelRun(self, request, context):  # noqa: N802
        self._op.cancel_run(request.run_id)
        return pb.Empty()

    def GetRunResult(self, request, context):  # noqa: N802
        try:
            payload = self._op._get_run_result_payload(request.run_id)
        except KeyError:
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Run {request.run_id} not found",
            )
        except (RunResultNotReadyError, RunResultUnavailableError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            context.abort(grpc.StatusCode.DATA_LOSS, str(exc))
        return pb.RunResultMsg(
            value_json=payload.value_json,
            files=[_result_file_attachment_to_proto(item) for item in payload.files],
        )

    def ListRunSummaries(self, request, context):  # noqa: N802
        try:
            page = self._op.list_run_summaries(
                request.workflow_selector,
                page_size=request.page_size,
                page_token=request.page_token,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return pb.RunSummaryPage(
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
            runs=[run_summary_to_proto(item) for item in page.runs],
            next_page_token=page.next_page_token,
        )

    def GetRunSnapshot(self, request, context):  # noqa: N802
        try:
            snapshot = self._op.get_run_snapshot(
                request.run_id,
                operator_instance_id=request.operator_instance_id,
                as_of_sequence=request.as_of_sequence,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        if snapshot is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return run_snapshot_to_proto(snapshot)

    def GetLatestRunSnapshot(self, request, context):  # noqa: N802
        try:
            snapshot = self._op.get_latest_run_snapshot(
                request.run_id,
                operator_instance_id=request.operator_instance_id,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        if snapshot is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return run_snapshot_to_proto(snapshot)

    def ListLogs(self, request, context):  # noqa: N802
        if not request.page_token:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "page_token from GetRunSnapshot is required",
            )
        try:
            page = self._op.list_logs(
                page_token=request.page_token,
                after_sequence=request.after_sequence,
                page_size=request.page_size,
                before_sequence=request.before_sequence,
                node_id=request.node_id,
                order=request.order,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Log target not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return pb.LogPage(
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
            logs=[log_record_descriptor_to_proto(item) for item in page.logs],
            next_page_token=page.next_page_token,
        )

    def ListAgentEvents(self, request, context):  # noqa: N802
        if not request.page_token:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "page_token from GetRunSnapshot is required",
            )
        try:
            page = self._op.list_agent_events(
                page_token=request.page_token,
                after_event_sequence=request.after_event_sequence,
                page_size=request.page_size,
                before_event_sequence=request.before_event_sequence,
                order=request.order,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Agent event target not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return pb.AgentEventPage(
            operator_instance_id=page.operator_instance_id,
            as_of_sequence=page.as_of_sequence,
            run_id=page.run_id,
            node_id=page.node_id,
            events=[agent_event_descriptor_to_proto(item) for item in page.events],
            next_page_token=page.next_page_token,
        )

    def ReadTrace(self, request, context):  # noqa: N802
        try:
            trace = self._op.read_trace(
                request.run_id,
                request.node_id,
                operator_instance_id=request.operator_instance_id,
                revision=request.revision,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"Trace for run {request.run_id}, node {request.node_id}, "
                f"revision {request.revision or 'latest'} not found",
            )
        for chunk_index, offset in enumerate(range(0, len(trace.data), TRACE_CHUNK_BYTES)):
            data = trace.data[offset : offset + TRACE_CHUNK_BYTES]
            yield pb.TraceChunk(
                revision=trace.revision,
                chunk_index=chunk_index,
                data=data,
                eof=offset + len(data) == len(trace.data),
            )

    def ReadDetail(self, request, context):  # noqa: N802
        try:
            data = self._op.read_detail(request.body_token)
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Detail body not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if not data:
            yield pb.DetailChunk(chunk_index=0, data=b"", eof=True)
            return
        for chunk_index, offset in enumerate(range(0, len(data), TRACE_CHUNK_BYTES)):
            chunk = data[offset : offset + TRACE_CHUNK_BYTES]
            yield pb.DetailChunk(
                chunk_index=chunk_index,
                data=chunk,
                eof=offset + len(chunk) == len(data),
            )

    def StreamOperatorUpdates(self, request, context):  # noqa: N802
        """Replay typed operator updates for one epoch, or require a reset."""
        subscription = self._op.subscribe_operator_updates(
            request.operator_instance_id,
            request.after_sequence,
        )
        try:
            context.send_initial_metadata(())
            while context.is_active():
                try:
                    envelope = subscription.get(timeout=1.0)
                except queue.Empty:
                    continue
                yield operator_update_envelope_to_proto(envelope)
                if envelope.reset_required is not None:
                    return
        finally:
            self._op.unsubscribe_operator_updates(subscription)


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
        pb_grpc.add_OperatorServiceServicer_to_server(OperatorServicer(operator), server)
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


def _listen_address(host: str, port: int) -> str:
    if type(host) is not str or not host:
        raise ValueError("Operator listen host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Operator listen port must be an integer from 0 to 65535")
    normalized = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    display_host = f"[{normalized}]" if ":" in normalized else normalized
    return f"{display_host}:{port}"


def _result_file_attachment_to_proto(item) -> pb.ResultFileAttachment:
    message = pb.ResultFileAttachment(
        attachment_id=item.attachment_id,
        content=item.content,
        sha256=item.sha256,
    )
    if item.name is not None:
        message.name = item.name
    if item.media_type is not None:
        message.media_type = item.media_type
    return message


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


def _decode_input_payload(request) -> dict[str, Any] | None:
    payload = _decode_json_object(request.input_json, "input_json") or {}
    for file in request.input_files:
        if not file.field_name:
            raise ValueError("input file attachment is missing field_name")
        _set_input_field(
            payload,
            file.field_name,
            File(
                name=file.name or None,
                content=bytes(file.content),
                content_type=file.content_type or None,
                sha256=file.sha256 or None,
            ),
        )
    return payload or None


def _set_input_field(payload: dict[str, Any], field_name: str, value: Any) -> None:
    if field_name in payload:
        raise ValueError(f"Duplicate input field '{field_name}'")
    payload[field_name] = value


def _ambiguous_detail(exc: AmbiguousWorkflow) -> str:
    candidates = "\n".join(f"  {item}" for item in sorted(exc.candidate_ids))
    return f"{exc.selector!r} is ambiguous:\n{candidates}"
