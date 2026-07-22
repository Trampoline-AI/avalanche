"""gRPC client that implements StateProvider for the TUI."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from numbers import Real
from typing import Any, Callable

import grpc
from pydantic import BaseModel

from avalanche.runtime import File

from ._grpc import _BOUNDED_MESSAGE_OPTIONS
from .convert import (
    agent_event_from_proto,
    discovery_diagnostic_from_proto,
    run_delta_envelope_from_proto,
    run_snapshot_from_proto,
    run_summary_from_proto,
    sequenced_log_entry_from_proto,
    workflow_info_from_proto,
)
from .models import (
    AgentEventAppended,
    LogAppended,
    LogEntry,
    NodeState,
    NodeStatusChanged,
    RunCreated,
    RunDeltaEnvelope,
    RunSnapshot,
    RunState,
    RunStatusChanged,
    RunSummary,
    TraceDetail,
    TraceFinalized,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
)
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc
from .results import (
    MAX_RESULT_ATTACHMENT_BYTES,
    MAX_RESULT_ATTACHMENTS,
    MAX_RESULT_ATTACHMENTS_BYTES,
    MAX_RESULT_TOTAL_BYTES,
    MAX_RESULT_VALUE_JSON_BYTES,
    EncodedWorkflowResult,
    ResultFileAttachment,
    decode_workflow_result,
)

DEFAULT_UNARY_TIMEOUT_SECONDS = 10.0
DETAIL_HYDRATION_PAGE_SIZE = 100
GET_RUN_SNAPSHOT_MAX_ATTEMPTS = 3
STREAM_THREAD_JOIN_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class _StreamCursor:
    operator_instance_id: str = ""
    sequence: int = 0


class _DeltaResetError(RuntimeError):
    pass


class _DetailHydrationRaceError(RuntimeError):
    pass


class GrpcStateProvider:
    """StateProvider backed by a remote gRPC OperatorService.

    Connects to an operator daemon and translates gRPC calls to the
    StateProvider interface that the TUI expects. Tracks connection
    state and retry attempts for the TUI to display.
    """

    def __init__(
        self,
        address: str = "localhost:7433",
        *,
        token: str | None = None,
        tls: bool = False,
        root_certificates: bytes | None = None,
        private_key: bytes | None = None,
        certificate_chain: bytes | None = None,
        unary_timeout: float = DEFAULT_UNARY_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(unary_timeout, bool) or not isinstance(unary_timeout, Real):
            raise TypeError("unary_timeout must be a real number")
        if not math.isfinite(unary_timeout) or unary_timeout <= 0:
            raise ValueError("unary_timeout must be positive and finite")

        self._address = address
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None
        self._unary_timeout = float(unary_timeout)
        if tls:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_certificates,
                private_key=private_key,
                certificate_chain=certificate_chain,
            )
            self._channel = grpc.secure_channel(
                address,
                credentials,
                options=_BOUNDED_MESSAGE_OPTIONS,
            )
        else:
            self._channel = grpc.insecure_channel(
                address,
                options=_BOUNDED_MESSAGE_OPTIONS,
            )
        self._stub = pb_grpc.OperatorServiceStub(self._channel)
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._closed = False
        self._cursor = _StreamCursor()
        self._runs_by_id: dict[str, RunState] = {}
        self._run_revisions: dict[str, int] = {}
        self._log_sequences: dict[str, int] = {}
        self._hydrated_log_runs: set[str] = set()
        self._node_revisions: dict[tuple[str, str], int] = {}
        self._agent_event_sequences: dict[tuple[str, str], int] = {}
        self._trace_revisions: dict[tuple[str, str], int] = {}
        self._hydrated_agent_nodes: set[tuple[str, str]] = set()
        self._hydrated_trace_revisions: dict[tuple[str, str], int] = {}
        self._legacy_names_by_workflow_id: dict[str, str] = {}

        # Connection state (read by TUI)
        self.connected: bool = False
        self.retry_count: int = 0
        self.last_error: str = ""
        self.discovery_diagnostics: list[WorkflowDiscoveryDiagnostic] = []

    @property
    def connection_label(self) -> str:
        """Human-readable remote endpoint for connection status displays."""
        return self._address

    def _call(self, fn, *args, default=None, **kwargs):
        """Wrap a gRPC call with connection state tracking."""
        kwargs.setdefault("timeout", self._unary_timeout)
        if self._metadata is not None and "metadata" not in kwargs:
            kwargs["metadata"] = self._metadata
        try:
            result = fn(*args, **kwargs)
            if not self.connected:
                self.retry_count = 0
            self.connected = True
            self.last_error = ""
            return result
        except grpc.RpcError as e:
            self.connected = False
            self.retry_count += 1
            self.last_error = f"{e.code().name}: {e.details()}"
            return default

    def list_workflows(self) -> list[WorkflowInfo]:
        resp = self._call(self._stub.ListFlows, pb.Empty())
        if resp is None:
            return []
        self._cache_legacy_workflow_names(resp)
        self.discovery_diagnostics = [
            discovery_diagnostic_from_proto(item) for item in resp.diagnostics
        ]
        return [workflow_info_from_proto(p) for p in resp.flows]

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        """List lightweight run summaries without detail bodies."""
        runs: list[RunState] = []
        page_token = ""
        while True:
            response = self._call(
                self._stub.ListRunSummaries,
                pb.ListRunSummariesRequest(
                    workflow_selector=workflow_selector,
                    page_size=1000,
                    page_token=page_token,
                ),
            )
            if response is None:
                return []
            runs.extend(
                _run_from_summary(
                    response.operator_instance_id,
                    run_summary_from_proto(item),
                )
                for item in response.runs
            )
            page_token = response.next_page_token
            if not page_token:
                return runs

    def get_run(self, run_id: str) -> RunState | None:
        """Fetch one pinned structural snapshot and lazily hydrate its details."""
        try:
            for attempt in range(GET_RUN_SNAPSHOT_MAX_ATTEMPTS):
                snapshot_cursor = self._materialize_structural_cursor()
                try:
                    snapshot = self._get_run_snapshot(
                        run_id,
                        snapshot_cursor.operator_instance_id,
                        snapshot_cursor.sequence,
                    )
                    break
                except grpc.RpcError as error:
                    if (
                        error.code() != grpc.StatusCode.FAILED_PRECONDITION
                        or attempt == GET_RUN_SNAPSHOT_MAX_ATTEMPTS - 1
                    ):
                        raise
            run = self._hydrate_run_snapshot(snapshot)
            self.connected = True
            self.retry_count = 0
            self.last_error = ""
            return run
        except _DetailHydrationRaceError:
            return None
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.NOT_FOUND:
                self.connected = True
                self.retry_count = 0
                self.last_error = ""
                return None
            self.connected = False
            self.last_error = f"{error.code().name}: {error.details()}"
            return None

    def get_run_result(self, run_id: str) -> Any:
        """Retrieve and decode a terminally successful workflow result.

        gRPC errors remain exceptions so callers can distinguish unknown,
        nonterminal, failed, and cancelled runs from a successful ``None``
        result.
        """
        kwargs = {"timeout": self._unary_timeout}
        if self._metadata is not None:
            kwargs["metadata"] = self._metadata
        try:
            response = self._stub.GetRunResult(
                pb.GetRunRequest(run_id=run_id),
                **kwargs,
            )
            _validate_wire_result_response(response)
            payload = EncodedWorkflowResult(
                value_json=response.value_json,
                files=tuple(
                    ResultFileAttachment(
                        attachment_id=item.attachment_id,
                        name=item.name if item.HasField("name") else None,
                        content=bytes(item.content),
                        media_type=(item.media_type if item.HasField("media_type") else None),
                        sha256=item.sha256,
                    )
                    for item in response.files
                ),
            )
            result = decode_workflow_result(payload)
        except grpc.RpcError as exc:
            self.connected = exc.code() in {
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.FAILED_PRECONDITION,
            }
            if self.connected:
                self.retry_count = 0
            else:
                self.retry_count += 1
            self.last_error = f"{exc.code().name}: {exc.details()}"
            raise
        except (TypeError, ValueError) as exc:
            self.connected = False
            self.retry_count += 1
            self.last_error = f"DATA_LOSS: {exc}"
            raise
        self.connected = True
        self.retry_count = 0
        self.last_error = ""
        return result
    def _materialize_structural_cursor(self) -> _StreamCursor:
        """Create one server-retained baseline and return its immutable cursor."""
        kwargs = {"timeout": self._unary_timeout}
        if self._metadata is not None:
            kwargs["metadata"] = self._metadata
        response = self._stub.ListRunSummaries(
            pb.ListRunSummariesRequest(page_size=1),
            **kwargs,
        )
        return _StreamCursor(
            operator_instance_id=response.operator_instance_id,
            sequence=response.as_of_sequence,
        )

    def _get_run_snapshot(
        self,
        run_id: str,
        operator_instance_id: str,
        as_of_sequence: int,
    ) -> RunSnapshot:
        """Fetch one snapshot pinned to the loader-selected epoch and high-water."""
        kwargs = {"timeout": self._unary_timeout}
        if self._metadata is not None:
            kwargs["metadata"] = self._metadata
        response = self._stub.GetRunSnapshot(
            pb.GetRunSnapshotRequest(
                run_id=run_id,
                operator_instance_id=operator_instance_id,
                as_of_sequence=as_of_sequence,
            ),
            **kwargs,
        )
        return run_snapshot_from_proto(response)

    def _hydrate_run_snapshot(self, snapshot: RunSnapshot) -> RunState:
        """Hydrate append-only details without weakening the structural baseline."""
        run = _run_from_snapshot(snapshot)
        with self._state_lock:
            starting_cursor = self._cursor
            cached = self._runs_by_id.get(run.run_id)
            reuse_cached = bool(
                cached is not None
                and cached.operator_instance_id == run.operator_instance_id
                and cached.created_sequence == run.created_sequence
            )
            logs_hydrated = reuse_cached and run.run_id in self._hydrated_log_runs
            log_sequence = (
                self._log_sequences.get(run.run_id, 0) if logs_hydrated else 0
            )
            if logs_hydrated and cached is not None:
                run.logs = deepcopy(cached.logs)

            hydrated_agent_nodes: set[tuple[str, str]] = set()
            agent_sequences: dict[tuple[str, str], int] = {}
            if reuse_cached and cached is not None:
                for node_id, node in run.nodes.items():
                    key = (run.run_id, node_id)
                    cached_node = cached.nodes.get(node_id)
                    if (
                        cached_node is not None
                        and key in self._hydrated_agent_nodes
                    ):
                        node.agent_trace_json = cached_node.agent_trace_json
                        cached_trace_revision = (
                            cached_node.trace.revision
                            if cached_node.trace is not None
                            else 0
                        )
                        snapshot_trace_revision = (
                            node.trace.revision if node.trace is not None else 0
                        )
                        if cached_trace_revision != snapshot_trace_revision:
                            _clear_trace_body(node)
                        hydrated_agent_nodes.add(key)
                        agent_sequences[key] = self._agent_event_sequences.get(key, 0)

        detail_as_of = snapshot.as_of_sequence
        if log_sequence < snapshot.latest_log_sequence:
            new_logs, log_sequence, detail_as_of = self._read_log_pages(
                run.run_id,
                operator_instance_id=snapshot.operator_instance_id,
                expected_as_of=detail_as_of,
                after_sequence=log_sequence,
            )
            run.logs.extend(item.entry for item in new_logs)
        if log_sequence < snapshot.latest_log_sequence:
            raise _DetailHydrationRaceError("log hydration ended below snapshot watermark")

        for node_id, node in run.nodes.items():
            if node.trace is None:
                continue
            key = (run.run_id, node_id)
            event_count = _agent_event_count(node)
            if (
                key not in hydrated_agent_nodes
                or event_count < node.trace.event_count
            ):
                events, event_sequence, detail_as_of = self._read_agent_event_pages(
                    run.run_id,
                    node_id,
                    operator_instance_id=snapshot.operator_instance_id,
                    expected_as_of=detail_as_of,
                    after_event_sequence=agent_sequences.get(key, 0),
                )
                for event in events:
                    _append_agent_event(node, event.event_json)
                agent_sequences[key] = event_sequence
                hydrated_agent_nodes.add(key)
                event_count = _agent_event_count(node)
            if event_count < node.trace.event_count:
                raise _DetailHydrationRaceError(
                    f"agent hydration ended below {run.run_id}/{node_id} watermark"
                )
            _finalize_agent_trace(node, node.trace.status)

        run.latest_log_sequence = log_sequence
        run.details_hydrated = True
        return self._commit_hydrated_run(
            snapshot,
            run,
            starting_cursor=starting_cursor,
            hydrated_agent_nodes=hydrated_agent_nodes,
            agent_sequences=agent_sequences,
        )

    def _read_log_pages(
        self,
        run_id: str,
        *,
        operator_instance_id: str,
        expected_as_of: int,
        after_sequence: int,
    ) -> tuple[list[Any], int, int]:
        logs = []
        cursor = after_sequence
        as_of_sequence = expected_as_of
        while True:
            response = self._stub.ListLogs(
                pb.ListLogsRequest(
                    run_id=run_id,
                    after_sequence=cursor,
                    page_size=DETAIL_HYDRATION_PAGE_SIZE,
                ),
                **self._detail_rpc_kwargs(),
            )
            as_of_sequence = self._validate_detail_page(
                response,
                operator_instance_id=operator_instance_id,
                expected_as_of=as_of_sequence,
            )
            page = [sequenced_log_entry_from_proto(item) for item in response.logs]
            for item in page:
                if item.sequence != cursor + 1:
                    raise _DetailHydrationRaceError("log page is not contiguous")
                cursor = item.sequence
            if response.next_sequence != cursor:
                raise _DetailHydrationRaceError("log page cursor does not match its payload")
            logs.extend(page)
            if not response.has_more:
                return logs, cursor, as_of_sequence
            if not page:
                raise _DetailHydrationRaceError("log pagination made no progress")

    def _read_agent_event_pages(
        self,
        run_id: str,
        node_id: str,
        *,
        operator_instance_id: str,
        expected_as_of: int,
        after_event_sequence: int,
    ) -> tuple[list[Any], int, int]:
        events = []
        cursor = after_event_sequence
        as_of_sequence = expected_as_of
        while True:
            response = self._stub.ListAgentEvents(
                pb.ListAgentEventsRequest(
                    run_id=run_id,
                    node_id=node_id,
                    after_event_sequence=cursor,
                    page_size=DETAIL_HYDRATION_PAGE_SIZE,
                ),
                **self._detail_rpc_kwargs(),
            )
            as_of_sequence = self._validate_detail_page(
                response,
                operator_instance_id=operator_instance_id,
                expected_as_of=as_of_sequence,
            )
            if response.run_id != run_id or response.node_id != node_id:
                raise _DetailHydrationRaceError("agent page identity changed")
            page = [agent_event_from_proto(item) for item in response.events]
            for item in page:
                if item.event_sequence <= cursor:
                    raise _DetailHydrationRaceError("agent event sequence is not increasing")
                cursor = item.event_sequence
            if response.next_event_sequence != cursor:
                raise _DetailHydrationRaceError(
                    "agent page cursor does not match its payload"
                )
            events.extend(page)
            if not response.has_more:
                return events, cursor, as_of_sequence
            if not page:
                raise _DetailHydrationRaceError("agent pagination made no progress")

    def _detail_rpc_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self._unary_timeout}
        if self._metadata is not None:
            kwargs["metadata"] = self._metadata
        return kwargs

    @staticmethod
    def _validate_detail_page(
        response: Any,
        *,
        operator_instance_id: str,
        expected_as_of: int,
    ) -> int:
        if response.operator_instance_id != operator_instance_id:
            raise _DetailHydrationRaceError("operator epoch changed during hydration")
        if response.as_of_sequence != expected_as_of:
            raise _DetailHydrationRaceError("detail high-water changed during hydration")
        return response.as_of_sequence

    def _commit_hydrated_run(
        self,
        snapshot: RunSnapshot,
        hydrated: RunState,
        *,
        starting_cursor: _StreamCursor,
        hydrated_agent_nodes: set[tuple[str, str]],
        agent_sequences: dict[tuple[str, str], int],
    ) -> RunState:
        with self._state_lock:
            current_cursor = self._cursor
            if (
                starting_cursor.operator_instance_id
                and starting_cursor.operator_instance_id
                != snapshot.operator_instance_id
            ):
                raise _DetailHydrationRaceError("snapshot epoch does not match client epoch")
            if (
                current_cursor.operator_instance_id
                and current_cursor.operator_instance_id
                != snapshot.operator_instance_id
            ):
                raise _DetailHydrationRaceError("client epoch changed during hydration")

            current = self._runs_by_id.get(hydrated.run_id)
            if current is not None:
                if (
                    current.operator_instance_id != hydrated.operator_instance_id
                    or current.created_sequence != hydrated.created_sequence
                ):
                    raise _DetailHydrationRaceError("run identity changed during hydration")
                if current.latest_log_sequence > hydrated.latest_log_sequence:
                    raise _DetailHydrationRaceError("logs advanced during hydration")
                for node_id, node in current.nodes.items():
                    descriptor = node.trace
                    hydrated_node = hydrated.nodes.get(node_id)
                    current_trace_revision = (
                        descriptor.revision if descriptor is not None else 0
                    )
                    hydrated_descriptor = (
                        hydrated_node.trace if hydrated_node is not None else None
                    )
                    hydrated_trace_revision = (
                        hydrated_descriptor.revision
                        if hydrated_descriptor is not None
                        else 0
                    )
                    if current_trace_revision != hydrated_trace_revision:
                        raise _DetailHydrationRaceError(
                            f"trace descriptor advanced during hydration for {node_id}"
                        )
                    if (
                        descriptor is not None
                        and hydrated_node is not None
                        and descriptor.event_count
                        > _agent_event_count(hydrated_node)
                    ):
                        raise _DetailHydrationRaceError(
                            f"agent events advanced during hydration for {node_id}"
                        )
                structural = (
                    current if current.revision > hydrated.revision else hydrated
                )
                result = deepcopy(structural)
                result.logs = hydrated.logs
                result.latest_log_sequence = hydrated.latest_log_sequence
                for node_id, node in hydrated.nodes.items():
                    if node_id in result.nodes:
                        result.nodes[node_id].agent_trace_json = node.agent_trace_json
                result.details_hydrated = True
            else:
                result = hydrated

            self._runs_by_id[result.run_id] = result
            self._run_revisions[result.run_id] = max(
                self._run_revisions.get(result.run_id, 0), result.revision
            )
            for node_id, node in result.nodes.items():
                key = (result.run_id, node_id)
                self._node_revisions[key] = max(
                    self._node_revisions.get(key, 0), node.revision
                )
                if node.trace is not None:
                    self._trace_revisions[key] = max(
                        self._trace_revisions.get(key, 0), node.trace.revision
                    )
            self._hydrated_log_runs.add(result.run_id)
            self._log_sequences[result.run_id] = hydrated.latest_log_sequence
            for key in hydrated_agent_nodes:
                self._hydrated_agent_nodes.add(key)
                self._agent_event_sequences[key] = agent_sequences.get(key, 0)
            return deepcopy(result)

    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail | None:
        """Hydrate and return one identity-pinned trace body."""
        try:
            run = self._hydrate_trace(run_id, node_id)
        except _DetailHydrationRaceError:
            return None
        return _trace_detail_from_run(run, node_id)

    def _hydrate_trace(self, run_id: str, node_id: str) -> RunState | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        node = run.nodes.get(node_id)
        descriptor = node.trace if node is not None else None
        if descriptor is None or not descriptor.available:
            return run
        key = (run_id, node_id)
        with self._state_lock:
            if self._hydrated_trace_revisions.get(key) == descriptor.revision:
                return deepcopy(self._runs_by_id.get(run_id, run))

        chunks = self._stub.ReadTrace(
            pb.ReadTraceRequest(
                operator_instance_id=run.operator_instance_id,
                run_id=run_id,
                node_id=node_id,
                revision=descriptor.revision,
            ),
            **self._detail_rpc_kwargs(),
        )
        data = bytearray()
        saw_eof = False
        for expected_index, chunk in enumerate(chunks):
            if saw_eof:
                raise _DetailHydrationRaceError("trace stream continued after eof")
            if (
                chunk.revision != descriptor.revision
                or chunk.chunk_index != expected_index
            ):
                raise _DetailHydrationRaceError("trace chunk identity changed")
            data.extend(chunk.data)
            saw_eof = chunk.eof
        if not saw_eof or len(data) != descriptor.size_bytes:
            raise _DetailHydrationRaceError("trace body does not match its descriptor")
        try:
            trace = json.loads(data)
        except (TypeError, ValueError) as error:
            raise _DetailHydrationRaceError("trace body is not valid JSON") from error
        if not isinstance(trace, dict):
            raise _DetailHydrationRaceError("trace body is not a JSON object")

        with self._state_lock:
            current_cursor = self._cursor
            current = self._runs_by_id.get(run_id)
            current_node = current.nodes.get(node_id) if current is not None else None
            current_descriptor = current_node.trace if current_node is not None else None
            if (
                current is None
                or current.operator_instance_id != run.operator_instance_id
                or current.created_sequence != run.created_sequence
                or (
                    current_cursor.operator_instance_id
                    and current_cursor.operator_instance_id
                    != run.operator_instance_id
                )
                or current_descriptor is None
                or current_descriptor.revision != descriptor.revision
            ):
                return None
            result = deepcopy(current)
            _install_trace_body(result.nodes[node_id], trace)
            self._runs_by_id[run_id] = result
            self._hydrated_trace_revisions[key] = descriptor.revision
            return deepcopy(result)

    def start_run(
        self,
        workflow_selector: str,
        *,
        run_id: str | None = None,
        input: Mapping[str, Any] | BaseModel | None = None,
        context: Mapping[str, Any] | BaseModel | None = None,
        files: Mapping[str, File | bytes] | None = None,
    ) -> str:
        input_files = [
            _file_attachment(field_name, value) for field_name, value in (files or {}).items()
        ]
        flow_name = self._legacy_names_by_workflow_id.get(workflow_selector, workflow_selector)
        request = pb.StartRunRequest(
            flow_name=flow_name,
            workflow_selector=workflow_selector,
            run_id=run_id or "",
            input_json=_json_payload(input),
            context_json=_json_payload(context),
            input_files=input_files,
        )
        resp = self._call(self._stub.StartRun, request)
        return resp.run_id if resp else ""

    def cancel_run(self, run_id: str) -> None:
        self._call(self._stub.CancelRun, pb.CancelRunRequest(run_id=run_id))

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._run_callbacks.append(callback)
        self._ensure_stream()

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._log_callbacks.append(callback)

    def _ensure_stream(self) -> None:
        """Start the background streaming thread if not already running."""
        with self._lifecycle_lock:
            if self._closed or self._stream_stop.is_set():
                return
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
            self._stream_thread.start()

    def ping(self) -> bool:
        """Quick health check — try a fast unary call with short timeout."""
        try:
            kwargs = {"timeout": min(2.0, self._unary_timeout)}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            resp = self._stub.ListFlows(pb.Empty(), **kwargs)
            self._cache_legacy_workflow_names(resp)
            if not self.connected:
                self.retry_count = 0
            self.connected = True
            self.last_error = ""
            return True
        except grpc.RpcError as e:
            self.connected = False
            self.retry_count += 1
            self.last_error = f"{e.code().name}: {e.details()}"
            return False

    def _cache_legacy_workflow_names(self, response: pb.FlowList) -> None:
        self._legacy_names_by_workflow_id = {
            (item.workflow_id or item.name): (item.name or item.display_name)
            for item in response.flows
        }

    def _stream_loop(self) -> None:
        """Consume ordered run deltas and publish materialized structural state."""
        while not self._stream_stop.is_set():
            try:
                self.retry_count += 1
                if self._stream_stop.is_set():
                    break
                with self._state_lock:
                    cursor = self._cursor
                stream = self._stub.StreamRunDeltas(
                    pb.StreamRunDeltasRequest(
                        operator_instance_id=cursor.operator_instance_id,
                        after_sequence=cursor.sequence,
                    ),
                    metadata=self._metadata,
                )
                with self._lifecycle_lock:
                    if self._closed:
                        break
                    self.connected = True
                    self.retry_count = 0
                    self.last_error = ""
                reconnect = False
                for message in stream:
                    if self._stream_stop.is_set():
                        break
                    envelope = run_delta_envelope_from_proto(message)
                    if envelope.reset_required is not None:
                        self._reload_structural_state()
                        reconnect = True
                        break
                    try:
                        run, log = self._apply_delta_envelope(envelope)
                    except _DeltaResetError:
                        self._reload_structural_state()
                        reconnect = True
                        break
                    if log is not None:
                        self._notify_log_callbacks(log)
                    if run is not None:
                        self._notify_run_callbacks(run)
                if reconnect:
                    continue
            except grpc.RpcError as e:
                if self._stream_stop.is_set():
                    break
                self.connected = False
                self.last_error = f"{e.code().name}: {e.details()}"
                delay = min(2 ** min(self.retry_count, 5), 30)
                self._stream_stop.wait(delay)
            except Exception as e:
                if self._stream_stop.is_set():
                    break
                self.connected = False
                self.last_error = str(e)
                self._stream_stop.wait(2.0)

    def _apply_delta_envelope(
        self, envelope: RunDeltaEnvelope
    ) -> tuple[RunState | None, LogEntry | None]:
        with self._state_lock:
            return self._apply_delta_envelope_locked(envelope)

    def _apply_delta_envelope_locked(
        self, envelope: RunDeltaEnvelope
    ) -> tuple[RunState | None, LogEntry | None]:
        delta = envelope.delta
        if delta is None:
            raise _DeltaResetError("delta payload missing")
        cursor = self._cursor
        operator_instance_id = cursor.operator_instance_id
        if operator_instance_id:
            if envelope.operator_instance_id != operator_instance_id:
                raise _DeltaResetError("operator epoch changed")
        else:
            operator_instance_id = envelope.operator_instance_id

        if delta.sequence <= cursor.sequence:
            return None, None
        if delta.sequence != cursor.sequence + 1:
            raise _DeltaResetError("delta sequence gap")

        change = delta.change
        log: LogEntry | None = None
        if isinstance(change, RunCreated):
            run = _run_from_created(envelope.operator_instance_id, change)
            old_revision = self._run_revisions.get(run.run_id, -1)
            if change.summary.revision <= old_revision:
                run = None
            else:
                self._runs_by_id[run.run_id] = run
                self._run_revisions[run.run_id] = change.summary.revision
                for node in run.nodes.values():
                    self._node_revisions[(run.run_id, node.node_id)] = node.revision
                self._hydrated_log_runs.add(run.run_id)
                self._log_sequences[run.run_id] = 0
        else:
            current = self._runs_by_id.get(change.run_id)
            if current is None:
                raise _DeltaResetError(f"delta references unknown run {change.run_id}")
            run = deepcopy(current)
            if isinstance(change, RunStatusChanged):
                old_revision = self._run_revisions.get(change.run_id, 0)
                if change.revision <= old_revision:
                    run = None
                else:
                    run.status = change.status
                    run.started_at = change.started_at
                    run.ended_at = change.ended_at
                    run.revision = change.revision
                    self._run_revisions[change.run_id] = change.revision
            elif isinstance(change, NodeStatusChanged):
                node = run.nodes.get(change.node_id)
                if node is None:
                    raise _DeltaResetError(
                        f"delta references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                if change.revision <= self._node_revisions.get(key, 0):
                    run = None
                else:
                    node.status = change.status
                    node.started_at = change.started_at
                    node.ended_at = change.ended_at
                    node.revision = change.revision
                    self._node_revisions[key] = change.revision
            elif isinstance(change, LogAppended):
                logs_hydrated = change.run_id in self._hydrated_log_runs
                known_sequence = self._log_sequences.get(change.run_id, 0)
                if logs_hydrated and change.log.sequence <= known_sequence:
                    run = None
                else:
                    run.logs.append(change.log.entry)
                    run.latest_log_sequence = max(
                        run.latest_log_sequence, change.log.sequence
                    )
                    if logs_hydrated:
                        self._log_sequences[change.run_id] = change.log.sequence
                    log = change.log.entry
            elif isinstance(change, AgentEventAppended):
                node = run.nodes.get(change.node_id)
                if node is None:
                    raise _DeltaResetError(
                        f"delta references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                node_hydrated = key in self._hydrated_agent_nodes
                known_sequence = self._agent_event_sequences.get(key, 0)
                if node_hydrated and change.event.event_sequence <= known_sequence:
                    run = None
                else:
                    was_empty = _agent_event_count(node) == 0
                    _append_agent_event(node, change.event.event_json)
                    if node_hydrated or (
                        was_empty and change.event.event_sequence == 1
                    ):
                        self._hydrated_agent_nodes.add(key)
                        self._agent_event_sequences[key] = (
                            change.event.event_sequence
                        )
            elif isinstance(change, TraceFinalized):
                node = run.nodes.get(change.node_id)
                if node is None:
                    raise _DeltaResetError(
                        f"delta references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                if change.trace.revision <= self._trace_revisions.get(key, 0):
                    run = None
                else:
                    _clear_trace_body(node)
                    node.trace = change.trace
                    node.revision = max(node.revision, change.trace.revision)
                    _finalize_agent_trace(node, change.trace.status)
                    if (
                        self._hydrated_trace_revisions.get(key)
                        != change.trace.revision
                    ):
                        self._hydrated_trace_revisions.pop(key, None)
                    self._trace_revisions[key] = change.trace.revision
            else:
                raise _DeltaResetError("unsupported delta change")

            if run is not None:
                run.revision = max(run.revision, delta.sequence)
                self._runs_by_id[run.run_id] = run

        self._cursor = _StreamCursor(operator_instance_id, delta.sequence)
        return run, log

    def _reload_structural_state(self) -> None:
        """Install the exact baseline returned by the authoritative loader."""
        operator_instance_id, as_of_sequence, runs = (
            self._load_authoritative_structural_baseline(self._get_run_snapshot)
        )
        self._install_structural_baseline(
            operator_instance_id,
            as_of_sequence,
            runs,
        )

    def _load_authoritative_structural_baseline(
        self,
        load_snapshot: Callable[[str, str, int], RunSnapshot],
    ) -> tuple[str, int, dict[str, RunState]]:
        """Health-owned loader; every snapshot must use its exact epoch/high-water."""
        del load_snapshot
        raise _DeltaResetError(
            "authoritative structural baseline loader is not installed"
        )

    def _install_structural_baseline(
        self,
        operator_instance_id: str,
        as_of_sequence: int,
        runs: dict[str, RunState],
    ) -> None:
        """Atomically acknowledge one loader-validated epoch and high-water."""
        with self._state_lock:
            for run in runs.values():
                run.details_hydrated = False
            self._runs_by_id = runs
            self._run_revisions = {
                run_id: run.revision for run_id, run in runs.items()
            }
            self._node_revisions = {
                (run_id, node_id): node.revision
                for run_id, run in runs.items()
                for node_id, node in run.nodes.items()
            }
            self._log_sequences.clear()
            self._hydrated_log_runs.clear()
            self._agent_event_sequences.clear()
            self._hydrated_agent_nodes.clear()
            self._trace_revisions = {
                (run_id, node_id): node.trace.revision
                for run_id, run in runs.items()
                for node_id, node in run.nodes.items()
                if node.trace is not None
            }
            self._hydrated_trace_revisions.clear()
            self._cursor = _StreamCursor(operator_instance_id, as_of_sequence)
            installed = tuple(runs.values())
        for run in installed:
            self._notify_run_callbacks(run)

    def _notify_run_callbacks(self, run: RunState) -> None:
        for callback in self._run_callbacks:
            try:
                callback(run)
            except Exception:
                pass

    def _notify_log_callbacks(self, log: LogEntry) -> None:
        for callback in self._log_callbacks:
            try:
                callback(log)
            except Exception:
                pass

    def close(self) -> None:
        """Stop stream reconnects and close the gRPC channel."""
        with self._lifecycle_lock:
            close_channel = not self._closed
            self._closed = True
            self._stream_stop.set()
            self.connected = False
            thread = self._stream_thread
        if close_channel:
            self._channel.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STREAM_THREAD_JOIN_TIMEOUT_SECONDS)
        self.connected = False


def _run_from_summary(
    operator_instance_id: str,
    summary: RunSummary,
) -> RunState:
    """Mark list projections so consumers merge metadata into hydrated detail."""
    run = _run_from_created(
        operator_instance_id,
        RunCreated(summary=summary),
    )
    run.details_hydrated = False
    return run


def _run_from_created(operator_instance_id: str, created: RunCreated) -> RunState:
    summary = created.summary
    run = RunState(
        run_id=summary.run_id,
        flow_name=summary.flow_name,
        status=summary.status,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        triggered_by=summary.triggered_by,
        workflow_id=summary.workflow_id,
        workflow_display_name=summary.workflow_display_name,
        operator_instance_id=operator_instance_id,
        created_sequence=summary.created_sequence,
        revision=summary.revision,
    )
    run.nodes = {
        item.node_id: NodeState(
            node_id=item.node_id,
            name=item.name,
            node_type=item.node_type,
            status=item.status,
            started_at=item.started_at,
            ended_at=item.ended_at,
            trace=item.trace,
            revision=item.revision,
        )
        for item in created.nodes
    }
    return run


def _run_from_snapshot(snapshot: RunSnapshot) -> RunState:
    """Materialize the authoritative structural baseline used by the reducer."""
    run = _run_from_created(
        snapshot.operator_instance_id,
        RunCreated(summary=snapshot.summary, nodes=snapshot.nodes),
    )
    run.latest_log_sequence = snapshot.latest_log_sequence
    run.details_hydrated = False
    return run


def _agent_event_count(node: NodeState) -> int:
    if not node.agent_trace_json:
        return 0
    try:
        envelope = json.loads(node.agent_trace_json)
    except (TypeError, ValueError):
        return 0
    events = envelope.get("events") if isinstance(envelope, dict) else None
    return len(events) if isinstance(events, list) else 0


def _clear_trace_body(node: NodeState) -> None:
    if not node.agent_trace_json:
        return
    try:
        loaded = json.loads(node.agent_trace_json)
    except (TypeError, ValueError):
        return
    if not isinstance(loaded, dict):
        return
    loaded["trace"] = None
    node.agent_trace_json = json.dumps(loaded, default=str)


def _trace_detail_from_run(
    run: RunState | None, node_id: str
) -> TraceDetail | None:
    if run is None:
        return None
    node = run.nodes.get(node_id)
    descriptor = node.trace if node is not None else None
    if node is None or descriptor is None or not node.agent_trace_json:
        return None
    try:
        envelope = json.loads(node.agent_trace_json)
    except (TypeError, ValueError):
        return None
    trace_body = envelope.get("trace") if isinstance(envelope, dict) else None
    if not isinstance(trace_body, dict):
        return None
    return TraceDetail(
        operator_instance_id=run.operator_instance_id,
        run_id=run.run_id,
        created_sequence=run.created_sequence,
        node_id=node_id,
        descriptor_revision=descriptor.revision,
        trace_body=deepcopy(trace_body),
    )


def _install_trace_body(node: NodeState, trace: dict[str, Any]) -> None:
    if node.agent_trace_json:
        try:
            loaded = json.loads(node.agent_trace_json)
            envelope = loaded if isinstance(loaded, dict) else {}
        except (TypeError, ValueError):
            envelope = {}
    else:
        envelope = {}
    envelope.setdefault("schema_version", 1)
    envelope.setdefault("events", [])
    envelope["trace"] = trace
    envelope["status"] = str(trace.get("status") or "unavailable")
    evidence = trace.get("evidence")
    if isinstance(evidence, dict):
        envelope["run_id"] = evidence.get("run_id")
    envelope.setdefault("error", None)
    node.agent_trace_json = json.dumps(envelope, default=str)


def _append_agent_event(node: NodeState, event_json: str) -> None:
    try:
        event = json.loads(event_json)
    except json.JSONDecodeError:
        event = {"raw": event_json}
    if node.agent_trace_json is None:
        envelope: dict[str, Any] = {
            "schema_version": 1,
            "status": "in_progress",
            "run_id": None,
            "events": [],
            "trace": None,
            "error": None,
        }
    else:
        try:
            loaded = json.loads(node.agent_trace_json)
            envelope = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            envelope = {}
    events = envelope.get("events")
    if not isinstance(events, list):
        events = []
        envelope["events"] = events
    events.append(event)
    envelope["status"] = "in_progress"
    node.agent_trace_json = json.dumps(envelope, default=str)


def _finalize_agent_trace(node: NodeState, status: str) -> None:
    if node.agent_trace_json is None:
        envelope: dict[str, Any] = {
            "schema_version": 1,
            "events": [],
            "trace": None,
            "error": None,
        }
    else:
        try:
            loaded = json.loads(node.agent_trace_json)
            envelope = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            envelope = {}
    envelope["status"] = status
    node.agent_trace_json = json.dumps(envelope, default=str)


def _json_payload(payload: Mapping[str, Any] | BaseModel | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    return json.dumps(payload)


def _file_attachment(field_name: str, value: File | bytes) -> pb.FileAttachment:
    file = value if isinstance(value, File) else File(name=field_name, content=value)
    return pb.FileAttachment(
        field_name=field_name,
        name=file.name or "",
        content=file.content,
        content_type=file.content_type or "",
        sha256=file.sha256 or "",
    )


def _validate_wire_result_response(response: pb.RunResultMsg) -> None:
    value_size = len(response.value_json.encode("utf-8"))
    if value_size > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Workflow result JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    if len(response.files) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    total_attachment_bytes = 0
    for item in response.files:
        size = len(item.content)
        if size > MAX_RESULT_ATTACHMENT_BYTES:
            raise ValueError(
                f"Result file attachment exceeds {MAX_RESULT_ATTACHMENT_BYTES} bytes"
            )
        total_attachment_bytes += size
        if total_attachment_bytes > MAX_RESULT_ATTACHMENTS_BYTES:
            raise ValueError(
                "Workflow result file attachments exceed "
                f"{MAX_RESULT_ATTACHMENTS_BYTES} bytes"
            )
    if value_size + total_attachment_bytes > MAX_RESULT_TOTAL_BYTES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_TOTAL_BYTES} bytes")
