"""gRPC client that implements StateProvider for the TUI over OperatorServiceV2."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from numbers import Real
from typing import Any, Callable
from uuid import uuid4

import grpc
from pydantic import BaseModel

from avalanche.runtime import File
from avalanche.workspace import Workspace

from ._grpc import _BOUNDED_MESSAGE_OPTIONS
from .convert_v2 import (
    agent_event_descriptor_from_v2,
    catalog_snapshot_from_v2,
    log_record_descriptor_from_v2,
    operator_update_envelope_from_v2,
    run_snapshot_from_v2,
    run_summary_from_v2,
)
from .models import (
    AgentEvent,
    AgentEventAppended,
    AgentEventDetailAppended,
    CatalogReplaced,
    CatalogSnapshot,
    DetailUpdate,
    LogAppended,
    LogDetailAppended,
    LogEntry,
    NodeState,
    NodeStatusChanged,
    OperatorUpdateEnvelope,
    ResetBaseline,
    RunCreated,
    RunSnapshot,
    RunState,
    RunStatusChanged,
    RunSummary,
    SequencedLogEntry,
    StreamResetNotice,
    TraceDetail,
    TraceFinalized,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
    WorkflowReloadStatus,
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
RESET_BASELINE_PAGE_SIZE = 100
RESET_BASELINE_MAX_ATTEMPTS = 5
RESET_BASELINE_RETRY_SECONDS = 0.05
DEFAULT_MAX_DETAIL_BODY_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RETAINED_DETAIL_COUNT = 100_000
DEFAULT_MAX_RETAINED_DETAIL_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_PAGED_ITEMS = 100_000


class StreamState(str, Enum):
    """Lifecycle state for the live operator update stream."""

    CONNECTING = "connecting"
    REPLAYING = "replaying"
    LIVE = "live"
    RESET_REQUIRED = "reset_required"
    FAILED = "failed"
    STOPPED = "stopped"


_TRANSPORT_FAILURE_STATUSES = frozenset(
    {
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNAVAILABLE,
    }
)
_RESET_BASELINE_RETRY_STATUSES = _TRANSPORT_FAILURE_STATUSES | {
    grpc.StatusCode.ABORTED,
    grpc.StatusCode.FAILED_PRECONDITION,
    grpc.StatusCode.NOT_FOUND,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
}


class OperatorCallError(RuntimeError):
    """One failed unary operator operation with its gRPC status."""

    def __init__(self, status: grpc.StatusCode, details: str) -> None:
        self.status = status
        self.details = details
        super().__init__(f"{status.name}: {details}")


class _ClientBudgetExceededError(OperatorCallError):
    """A local client budget rejection that retries cannot make smaller."""


class StaleResetAcknowledgementError(RuntimeError):
    """A reset acknowledgement that no longer matches the pending generation."""


class _ResetBaselineMismatchError(RuntimeError):
    """One non-authoritative baseline attempt that must be retried."""


@dataclass(frozen=True)
class _StreamCursor:
    operator_instance_id: str = ""
    sequence: int = 0


@dataclass
class _DetailBudget:
    max_count: int
    max_bytes: int
    count: int = 0
    size_bytes: int = 0
    cache_keys: int = 0

    def reserve(self, count: int, size_bytes: int) -> None:
        if self.count + count > self.max_count:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "client detail hydration exceeds the configured retained body count limit",
            )
        if self.size_bytes + size_bytes > self.max_bytes:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "client detail hydration exceeds the configured retained byte limit",
            )
        self.count += count
        self.size_bytes += size_bytes

    def reserve_cache_key(self) -> None:
        if self.cache_keys + 1 > self.max_count:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "client detail hydration exceeds the configured retained cache key limit",
            )
        self.cache_keys += 1


_DetailCacheKey = tuple[str, str, str]


class _RunUpdateResetError(RuntimeError):
    pass


class _DetailHydrationRaceError(RuntimeError):
    pass


class GrpcStateProvider:
    """StateProvider backed by a remote gRPC OperatorServiceV2.

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
        max_detail_body_bytes: int = DEFAULT_MAX_DETAIL_BODY_BYTES,
        max_retained_detail_count: int = DEFAULT_MAX_RETAINED_DETAIL_COUNT,
        max_retained_detail_bytes: int = DEFAULT_MAX_RETAINED_DETAIL_BYTES,
        max_paged_items: int = DEFAULT_MAX_PAGED_ITEMS,
        reset_baseline_loader: Callable[[StreamResetNotice], ResetBaseline] | None = None,
    ) -> None:
        if isinstance(unary_timeout, bool) or not isinstance(unary_timeout, Real):
            raise TypeError("unary_timeout must be a real number")
        if not math.isfinite(unary_timeout) or unary_timeout <= 0:
            raise ValueError("unary_timeout must be positive and finite")
        if isinstance(max_detail_body_bytes, bool) or not isinstance(
            max_detail_body_bytes, int
        ):
            raise TypeError("max_detail_body_bytes must be an integer")
        if max_detail_body_bytes <= 0:
            raise ValueError("max_detail_body_bytes must be positive")
        self._validate_positive_integer(
            "max_retained_detail_count",
            max_retained_detail_count,
        )
        self._validate_positive_integer(
            "max_retained_detail_bytes",
            max_retained_detail_bytes,
        )
        self._validate_positive_integer("max_paged_items", max_paged_items)

        self._address = address
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None
        self._unary_timeout = float(unary_timeout)
        self._max_detail_body_bytes = max_detail_body_bytes
        self._max_retained_detail_count = max_retained_detail_count
        self._max_retained_detail_bytes = max_retained_detail_bytes
        self._max_paged_items = max_paged_items
        self._reset_baseline_loader = reset_baseline_loader
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
        self._stub = pb_grpc.OperatorServiceV2Stub(self._channel)
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._catalog_callbacks: list[Callable[[CatalogSnapshot], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._detail_callbacks: list[Callable[[DetailUpdate], None]] = []
        self._stream_reset_callbacks: list[Callable[[StreamResetNotice], None]] = []
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._reset_acknowledged = threading.Event()
        self._reset_acknowledged.set()
        self._closed = False
        self._cursor = _StreamCursor()
        self._event_cursor: pb.LifecycleCursorV2 | None = None
        self._runs_by_id: dict[str, RunState] = {}
        self._run_revisions: dict[str, int] = {}
        self._log_sequences: dict[str, int] = {}
        self._log_entries: dict[str, list[LogEntry]] = {}
        self._hydrated_log_runs: set[str] = set()
        self._node_revisions: dict[tuple[str, str], int] = {}
        self._agent_event_sequences: dict[tuple[str, str], int] = {}
        self._trace_revisions: dict[tuple[str, str], int] = {}
        self._hydrated_agent_nodes: set[tuple[str, str]] = set()
        self._hydrated_trace_revisions: dict[tuple[str, str], int] = {}
        self._activity_continuations: dict[tuple[str, str, str], pb.ContinuationRefV2] = {}
        self._detail_refs_by_key: dict[str, pb.ActivityDetailRefV2] = {}
        self._trace_detail_refs: dict[tuple[str, str, int], pb.ActivityDetailRefV2] = {}
        self._agent_events: dict[tuple[str, str], list[Any]] = {}
        self._trace_bodies: dict[tuple[str, str], dict[str, Any]] = {}
        self._detail_cache_usage: OrderedDict[_DetailCacheKey, tuple[int, int]] = OrderedDict()
        self._retained_detail_count = 0
        self._retained_detail_bytes = 0
        self._reset_generation: int = 0
        self._pending_reset: StreamResetNotice | None = None
        self._pending_event_cursor: pb.LifecycleCursorV2 | None = None
        self._validated_reset_baseline: ResetBaseline | None = None
        self._catalog = CatalogSnapshot()

        # Operator reachability is independent from live-update stream health.
        self.operator_instance_id: str = ""
        self.operator_reachable: bool = False
        self.retry_count: int = 0
        self.last_error: str = ""
        self.stream_state: StreamState = StreamState.STOPPED
        self.stream_retry_count: int = 0
        self.stream_error: str = ""
        self.discovery_diagnostics: list[WorkflowDiscoveryDiagnostic] = []

    @staticmethod
    def _validate_positive_integer(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    @property
    def connected(self) -> bool:
        """Whether the operator is reachable through unary RPCs."""
        return self.operator_reachable

    @property
    def connection_label(self) -> str:
        """Human-readable remote endpoint for connection status displays."""
        return self._address

    def _call(self, fn, *args, **kwargs):
        """Run one unary gRPC operation or raise its explicit operation error."""
        kwargs.setdefault("timeout", self._unary_timeout)
        if self._metadata is not None and "metadata" not in kwargs:
            kwargs["metadata"] = self._metadata
        try:
            result = fn(*args, **kwargs)
            self._record_unary_success()
            return result
        except grpc.RpcError as error:
            raise self._record_unary_error(error) from error

    def _record_unary_success(self) -> None:
        """Record one completed unary operation unless shutdown already won."""
        with self._lifecycle_lock:
            if self._closed:
                return
            if not self.operator_reachable:
                self.retry_count = 0
            self.operator_reachable = True
            self.last_error = ""

    def _record_unary_error(self, error: grpc.RpcError) -> OperatorCallError:
        """Classify one operation failure unless shutdown already won."""
        status = error.code()
        operation_error = OperatorCallError(status, error.details())
        with self._lifecycle_lock:
            if self._closed:
                return operation_error
            if status in _TRANSPORT_FAILURE_STATUSES:
                self.operator_reachable = False
                self.retry_count += 1
            else:
                self.operator_reachable = True
                self.retry_count = 0
            self.last_error = str(operation_error)
        return operation_error

    def get_catalog(self) -> CatalogSnapshot:
        flows: list[pb.FlowInfoV2] = []
        continuation: pb.ContinuationRefV2 | None = None
        page_count = 0
        while True:
            page_count += 1
            self._validate_page_accumulation(page_count, "flow pages")
            request = pb.DiscoverFlowsRequestV2(page_size=1000)
            if continuation is not None:
                request.continuation.CopyFrom(continuation)
            page = self._call(self._stub.DiscoverFlows, request)
            flows.extend(page.flows)
            if not page.next_page.continuation_id:
                catalog = catalog_snapshot_from_v2(page, flows=flows)
                break
            continuation = page.next_page
        with self._state_lock:
            self._install_catalog_locked(catalog)
        return catalog

    def list_workflows(self) -> list[WorkflowInfo]:
        return list(self.get_catalog().workflows)

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        """List lightweight run summaries without detail bodies."""
        runs: list[RunState] = []
        page_token: pb.ContinuationRefV2 | None = None
        seen_tokens: set[str] = set()
        page_count = 0
        while True:
            page_count += 1
            self._validate_page_accumulation(page_count, "run summary pages")
            request = pb.ListRunSummariesRequestV2(
                workflow_selector=workflow_selector,
                page_size=1000,
            )
            if page_token is not None:
                request.continuation.CopyFrom(page_token)
            response = self._call(self._stub.ListRunSummaries, request)
            if response is None:
                return []
            for item in response.runs:
                self._validate_page_accumulation(
                    len(runs) + 1,
                    "run summaries",
                )
                runs.append(
                    _run_from_summary(
                        response.scope_ref.reference,
                        run_summary_from_v2(item),
                    )
                )
            next_page = response.next_page
            if not next_page.continuation_id:
                runs.sort(key=lambda run: (run.created_sequence, run.run_id))
                return runs
            if next_page.continuation_id in seen_tokens:
                raise OperatorCallError(
                    grpc.StatusCode.DATA_LOSS,
                    "run summary pagination repeated a page token",
                )
            seen_tokens.add(next_page.continuation_id)
            page_token = next_page

    def _validate_page_accumulation(self, count: int, item_name: str) -> None:
        if count > self._max_paged_items:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"client {item_name} exceed the configured pagination item limit",
            )

    def get_latest_run_snapshot(
        self,
        run_id: str,
        operator_instance_id: str,
    ) -> RunSnapshot:
        """Fetch one latest structural snapshot pinned to an operator epoch."""
        response = self._call(
            self._stub.GetRunSnapshot,
            pb.GetRunSnapshotRequestV2(run_id=run_id),
        )
        self._remember_snapshot_bindings(response)
        snapshot = run_snapshot_from_v2(response)
        if operator_instance_id and snapshot.operator_instance_id != operator_instance_id:
            raise OperatorCallError(
                grpc.StatusCode.FAILED_PRECONDITION,
                "operator epoch changed while fetching the latest run snapshot",
            )
        return snapshot

    def get_run(self, run_id: str) -> RunState | None:
        """Fetch one pinned structural snapshot and lazily hydrate its details."""
        last_race: _DetailHydrationRaceError | None = None
        for attempt in range(GET_RUN_SNAPSHOT_MAX_ATTEMPTS):
            snapshot_requested = False
            snapshot_received = False
            try:
                snapshot_cursor = self._materialize_structural_cursor()
                snapshot_requested = True
                snapshot = self._get_run_snapshot(
                    run_id,
                    snapshot_cursor.operator_instance_id,
                    snapshot_cursor.sequence,
                )
                snapshot_received = True
                return self._hydrate_run_snapshot(snapshot)
            except OperatorCallError as error:
                if (
                    error.status is grpc.StatusCode.NOT_FOUND
                    and snapshot_requested
                    and not snapshot_received
                ):
                    return None
                if (
                    error.status is grpc.StatusCode.FAILED_PRECONDITION
                    and attempt < GET_RUN_SNAPSHOT_MAX_ATTEMPTS - 1
                ):
                    continue
                raise
            except _DetailHydrationRaceError as error:
                last_race = error
                if attempt < GET_RUN_SNAPSHOT_MAX_ATTEMPTS - 1:
                    continue
                raise
        assert last_race is not None
        raise last_race

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
                pb.GetRunResultRequestV2(run_id=run_id),
                **kwargs,
            )
            value_bytes = response.value.value_json.encode("utf-8")
            if len(value_bytes) != response.value.size_bytes:
                raise ValueError("result value does not match its advertised size")
            if hashlib.sha256(value_bytes).hexdigest() != response.value.sha256:
                raise ValueError("result value does not match its advertised digest")
            files = tuple(
                ResultFileAttachment(
                    attachment_id=item.artifact_ref.artifact_id,
                    name=item.name if item.HasField("name") else None,
                    content=self._read_artifact_body(item.artifact_ref),
                    media_type=item.media_type if item.HasField("media_type") else None,
                    sha256=item.artifact_ref.sha256,
                )
                for item in response.files
            )
            _validate_wire_result_payload(response.value.value_json, files)
            payload = EncodedWorkflowResult(
                value_json=response.value.value_json,
                files=files,
            )
            result = decode_workflow_result(payload)
        except grpc.RpcError as error:
            with self._lifecycle_lock:
                if not self._closed:
                    self.operator_reachable = error.code() not in _TRANSPORT_FAILURE_STATUSES
                    if self.operator_reachable:
                        self.retry_count = 0
                    else:
                        self.retry_count += 1
                    self.last_error = f"{error.code().name}: {error.details()}"
            raise
        except (TypeError, ValueError) as error:
            with self._lifecycle_lock:
                if not self._closed:
                    self.operator_reachable = True
                    self.retry_count = 0
                    self.last_error = f"DATA_LOSS: {error}"
            raise
        with self._lifecycle_lock:
            if not self._closed:
                self.operator_reachable = True
                self.retry_count = 0
                self.last_error = ""
        return result

    def _materialize_structural_cursor(self) -> _StreamCursor:
        """Create one server-retained baseline and return its immutable cursor."""
        response = self._call(
            self._stub.ListRunSummaries,
            pb.ListRunSummariesRequestV2(page_size=1),
        )
        return _StreamCursor(
            operator_instance_id=response.scope_ref.reference,
            sequence=response.cursor.source_sequence,
        )

    def _get_run_snapshot(
        self,
        run_id: str,
        operator_instance_id: str,
        as_of_sequence: int,
    ) -> RunSnapshot:
        """Fetch the latest snapshot, rejecting a stale operator epoch."""
        del as_of_sequence  # V2 snapshots are latest-view; epochs pin via scope.
        response = self._call(
            self._stub.GetRunSnapshot,
            pb.GetRunSnapshotRequestV2(run_id=run_id),
        )
        self._remember_snapshot_bindings(response)
        snapshot = run_snapshot_from_v2(response)
        if operator_instance_id and snapshot.operator_instance_id != operator_instance_id:
            raise OperatorCallError(
                grpc.StatusCode.FAILED_PRECONDITION,
                "operator epoch changed while fetching a run snapshot",
            )
        return snapshot

    def _read_artifact_body(self, artifact_ref: pb.RunOutputArtifactRefV2) -> bytes:
        """Stream one result artifact body, verifying size and digest."""
        chunks = self._stub.ReadRunOutputArtifact(
            pb.ReadRunOutputArtifactRequestV2(artifact_ref=artifact_ref),
            **self._detail_rpc_kwargs(),
        )
        try:
            data = bytearray()
            saw_eof = False
            for expected_index, chunk in enumerate(chunks):
                if saw_eof:
                    self._cancel_detail_stream(chunks)
                    raise ValueError("artifact stream continued after eof")
                if chunk.chunk_index != expected_index:
                    self._cancel_detail_stream(chunks)
                    raise ValueError("artifact chunk identity changed")
                data.extend(chunk.data)
                saw_eof = chunk.eof
        except grpc.RpcError:
            raise
        if not saw_eof or len(data) != artifact_ref.size_bytes:
            raise ValueError("artifact body does not match its descriptor")
        if hashlib.sha256(bytes(data)).hexdigest() != artifact_ref.sha256:
            raise ValueError("artifact body digest does not match its descriptor")
        return bytes(data)

    def _hydrate_run_snapshot(self, snapshot: RunSnapshot) -> RunState:
        """Hydrate append-only details without weakening the structural baseline."""
        run = _run_from_snapshot(snapshot)
        budget = self._new_detail_budget()
        budget.reserve_cache_key()
        with self._state_lock:
            starting_cursor = self._cursor
            cached = self._runs_by_id.get(run.run_id)
            reuse_cached = bool(
                cached is not None
                and cached.operator_instance_id == run.operator_instance_id
                and cached.created_sequence == run.created_sequence
            )
            logs_hydrated = reuse_cached and run.run_id in self._hydrated_log_runs
            log_sequence = self._log_sequences.get(run.run_id, 0) if logs_hydrated else 0
            logs = list(self._log_entries.get(run.run_id, ())) if logs_hydrated else []
            log_bytes = 0
            if logs_hydrated:
                log_usage = self._detail_cache_usage.get(
                    self._log_cache_key(run.run_id),
                    (len(logs), sum(len(log.message.encode()) for log in logs)),
                )
                budget.reserve(*log_usage)
                log_bytes = log_usage[1]

            hydrated_agent_nodes: set[tuple[str, str]] = set()
            agent_sequences: dict[tuple[str, str], int] = {}
            agent_events: dict[tuple[str, str], list[Any]] = {}
            agent_event_bytes: dict[tuple[str, str], int] = {}
            trace_bodies: dict[tuple[str, str], dict[str, Any]] = {}
            trace_body_bytes: dict[tuple[str, str], int] = {}
            if reuse_cached and cached is not None:
                for node_id, node in run.nodes.items():
                    key = (run.run_id, node_id)
                    cached_node = cached.nodes.get(node_id)
                    if cached_node is not None and key in self._hydrated_agent_nodes:
                        cached_trace_revision = (
                            cached_node.trace.revision if cached_node.trace is not None else 0
                        )
                        snapshot_trace_revision = (
                            node.trace.revision if node.trace is not None else 0
                        )
                        agent_events[key] = list(self._agent_events.get(key, ()))
                        event_usage = self._detail_cache_usage.get(
                            self._agent_cache_key(*key),
                            (
                                len(agent_events[key]),
                                sum(
                                    len(json.dumps(event, default=str).encode())
                                    for event in agent_events[key]
                                ),
                            ),
                        )
                        budget.reserve(*event_usage)
                        agent_event_bytes[key] = event_usage[1]
                        if (
                            cached_trace_revision == snapshot_trace_revision
                            and key in self._trace_bodies
                        ):
                            budget.reserve_cache_key()
                            trace_bodies[key] = self._trace_bodies[key]
                            trace_usage = self._detail_cache_usage.get(
                                self._trace_cache_key(*key),
                                (1, node.trace.size_bytes),
                            )
                            budget.reserve(*trace_usage)
                            trace_body_bytes[key] = trace_usage[1]
                        hydrated_agent_nodes.add(key)
                        agent_sequences[key] = self._agent_event_sequences.get(key, 0)

        detail_as_of = snapshot.as_of_sequence
        if log_sequence < snapshot.latest_log_sequence:
            new_logs, log_sequence = self._read_log_pages(
                snapshot.log_page_token,
                run_id=run.run_id,
                operator_instance_id=snapshot.operator_instance_id,
                expected_as_of=detail_as_of,
                after_sequence=log_sequence,
                budget=budget,
            )
            logs.extend(item.entry for item in new_logs)
            log_bytes += sum(item.size_bytes for item in new_logs)
        if log_sequence < snapshot.latest_log_sequence:
            raise _DetailHydrationRaceError("log hydration ended below snapshot watermark")

        for node_id, node in run.nodes.items():
            if node.trace is None:
                continue
            budget.reserve_cache_key()
            key = (run.run_id, node_id)
            node_events = agent_events.setdefault(key, [])
            event_count = len(node_events)
            if key not in hydrated_agent_nodes or event_count < node.trace.event_count:
                events, event_sequence = self._read_agent_event_pages(
                    node.event_page_token,
                    run.run_id,
                    node_id,
                    operator_instance_id=snapshot.operator_instance_id,
                    expected_as_of=detail_as_of,
                    after_event_sequence=agent_sequences.get(key, 0),
                    budget=budget,
                )
                for event in events:
                    _append_agent_event(node_events, event.event_json)
                agent_event_bytes[key] = agent_event_bytes.get(key, 0) + sum(
                    event.size_bytes for event in events
                )
                agent_sequences[key] = event_sequence
                hydrated_agent_nodes.add(key)
                event_count = len(node_events)
            if event_count < node.trace.event_count:
                raise _DetailHydrationRaceError(
                    f"agent hydration ended below {run.run_id}/{node_id} watermark"
                )

        run.latest_log_sequence = log_sequence
        run.details_hydrated = True
        return self._commit_hydrated_run(
            snapshot,
            run,
            logs=logs,
            log_bytes=log_bytes,
            starting_cursor=starting_cursor,
            hydrated_agent_nodes=hydrated_agent_nodes,
            agent_sequences=agent_sequences,
            agent_events=agent_events,
            agent_event_bytes=agent_event_bytes,
            trace_bodies=trace_bodies,
            trace_body_bytes=trace_body_bytes,
        )

    def _read_log_pages(
        self,
        page_token: str,
        *,
        run_id: str,
        operator_instance_id: str,
        expected_as_of: int,
        after_sequence: int,
        budget: _DetailBudget,
    ) -> tuple[list[SequencedLogEntry], int]:
        logs = []
        cursor = after_sequence
        continuation: pb.ContinuationRefV2 | None = self._activity_continuation_for(
            page_token,
            run_id=run_id,
            node_id="",
        )
        while True:
            request = pb.ListRunActivityRequestV2(
                run_id=run_id,
                page_size=DETAIL_HYDRATION_PAGE_SIZE,
            )
            if continuation is not None:
                request.continuation.CopyFrom(continuation)
            response = self._call(self._stub.ListRunActivity, request)
            self._validate_detail_page(
                response,
                operator_instance_id=operator_instance_id,
                expected_as_of=expected_as_of,
                expected_stream=f"activity:{run_id}:logs",
            )
            if response.run_id != run_id:
                raise _DetailHydrationRaceError("log page identity changed")
            page_had_items = False
            for item in response.activities:
                self._remember_detail_reference(item.detail_ref)
                descriptor = log_record_descriptor_from_v2(item)
                if descriptor.sequence <= cursor:
                    page_had_items = True
                    continue
                if descriptor.sequence != cursor + 1:
                    raise _DetailHydrationRaceError("log page is not contiguous")
                budget.reserve(1, descriptor.size_bytes)
                message = self._read_detail_body(
                    descriptor.body_token,
                    descriptor.size_bytes,
                    scope=operator_instance_id,
                ).decode()
                logs.append(
                    SequencedLogEntry(
                        sequence=descriptor.sequence,
                        entry=LogEntry(
                            timestamp=descriptor.timestamp,
                            level=descriptor.level,
                            node_id=descriptor.node_id,
                            message=message,
                        ),
                        size_bytes=descriptor.size_bytes,
                    )
                )
                cursor = descriptor.sequence
                page_had_items = True
            if not response.next_page.continuation_id:
                return logs, cursor
            if not page_had_items:
                raise _DetailHydrationRaceError("log pagination made no progress")
            self._remember_activity_continuation(
                response.next_page,
                run_id=run_id,
                node_id="",
            )
            continuation = self._activity_continuation_for(
                response.next_page.continuation_id,
                run_id=run_id,
                node_id="",
            )

    def _read_agent_event_pages(
        self,
        page_token: str,
        run_id: str,
        node_id: str,
        *,
        operator_instance_id: str,
        expected_as_of: int,
        after_event_sequence: int,
        budget: _DetailBudget,
    ) -> tuple[list[AgentEvent], int]:
        events = []
        cursor = after_event_sequence
        continuation: pb.ContinuationRefV2 | None = self._activity_continuation_for(
            page_token,
            run_id=run_id,
            node_id=node_id,
        )
        while True:
            request = pb.ListRunActivityRequestV2(
                run_id=run_id,
                node_id=node_id,
                page_size=DETAIL_HYDRATION_PAGE_SIZE,
            )
            if continuation is not None:
                request.continuation.CopyFrom(continuation)
            response = self._call(self._stub.ListRunActivity, request)
            self._validate_detail_page(
                response,
                operator_instance_id=operator_instance_id,
                expected_as_of=expected_as_of,
                expected_stream=f"activity:{run_id}:{node_id}",
            )
            if response.run_id != run_id:
                raise _DetailHydrationRaceError("agent page identity changed")
            page_had_items = False
            for item in response.activities:
                self._remember_detail_reference(item.detail_ref)
                descriptor = agent_event_descriptor_from_v2(item)
                if descriptor.event_sequence <= cursor:
                    page_had_items = True
                    continue
                budget.reserve(1, descriptor.size_bytes)
                event_json = self._read_detail_body(
                    descriptor.body_token,
                    descriptor.size_bytes,
                    scope=operator_instance_id,
                ).decode()
                events.append(
                    AgentEvent(
                        invocation_id=descriptor.invocation_id,
                        event_sequence=descriptor.event_sequence,
                        event_json=event_json,
                        size_bytes=descriptor.size_bytes,
                        event_kind=descriptor.event_kind,
                        iteration=descriptor.iteration,
                        duration_ms=descriptor.duration_ms,
                        error=descriptor.error,
                        tool_count=descriptor.tool_count,
                        predict_count=descriptor.predict_count,
                    )
                )
                cursor = descriptor.event_sequence
                page_had_items = True
            if not response.next_page.continuation_id:
                return events, cursor
            if not page_had_items:
                raise _DetailHydrationRaceError("agent pagination made no progress")
            self._remember_activity_continuation(
                response.next_page,
                run_id=run_id,
                node_id=node_id,
            )
            continuation = self._activity_continuation_for(
                response.next_page.continuation_id,
                run_id=run_id,
                node_id=node_id,
            )

    def _validate_detail_body_size(self, size_bytes: int) -> None:
        if size_bytes < 0 or size_bytes > self._max_detail_body_bytes:
            raise _DetailHydrationRaceError(
                "detail body exceeds the configured hydration byte limit"
            )

    @staticmethod
    def _copy_continuation(
        continuation: pb.ContinuationRefV2,
    ) -> pb.ContinuationRefV2:
        copied = pb.ContinuationRefV2()
        copied.CopyFrom(continuation)
        return copied

    @staticmethod
    def _copy_detail_reference(
        reference: pb.ActivityDetailRefV2,
    ) -> pb.ActivityDetailRefV2:
        copied = pb.ActivityDetailRefV2()
        copied.CopyFrom(reference)
        return copied

    @staticmethod
    def _copy_lifecycle_cursor(cursor: pb.LifecycleCursorV2) -> pb.LifecycleCursorV2:
        copied = pb.LifecycleCursorV2()
        copied.CopyFrom(cursor)
        return copied

    @staticmethod
    def _has_complete_lifecycle_cursor(cursor: pb.LifecycleCursorV2) -> bool:
        return bool(
            cursor.stream
            and cursor.topology_fingerprint
            and cursor.stream_generation
            and cursor.retained_floor
        )

    def _remember_activity_continuation(
        self,
        continuation: pb.ContinuationRefV2,
        *,
        run_id: str,
        node_id: str,
    ) -> None:
        if not continuation.continuation_id:
            return
        expected_stream = f"activity:{run_id}:{node_id or 'logs'}"
        if (
            continuation.scope_ref.reference == ""
            or continuation.cursor.stream != expected_stream
            or not self._has_complete_lifecycle_cursor(continuation.cursor)
        ):
            raise _DetailHydrationRaceError("activity continuation is not a complete binding")
        key = (run_id, node_id, continuation.continuation_id)
        with self._state_lock:
            existing = self._activity_continuations.get(key)
            if existing is not None and (
                not self._same_continuation(existing, continuation)
                or existing.cursor.stream != expected_stream
            ):
                raise _DetailHydrationRaceError("activity continuation binding was replaced")
            self._activity_continuations[key] = self._copy_continuation(continuation)

    @staticmethod
    def _same_continuation(
        left: pb.ContinuationRefV2,
        right: pb.ContinuationRefV2,
    ) -> bool:
        return (
            left.scope_ref.reference == right.scope_ref.reference
            and left.continuation_id == right.continuation_id
            and left.cursor.stream == right.cursor.stream
            and left.cursor.topology_fingerprint == right.cursor.topology_fingerprint
            and left.cursor.stream_generation == right.cursor.stream_generation
            and left.cursor.retained_floor == right.cursor.retained_floor
            and left.cursor.source_sequence == right.cursor.source_sequence
        )

    @staticmethod
    def _same_detail_reference(
        left: pb.ActivityDetailRefV2,
        right: pb.ActivityDetailRefV2,
    ) -> bool:
        return (
            left.run_id == right.run_id
            and left.scope_ref.reference == right.scope_ref.reference
            and left.activity_id == right.activity_id
            and left.run_sequence == right.run_sequence
            and left.object_uri == right.object_uri
            and left.object_key == right.object_key
            and left.sha256 == right.sha256
            and left.size_bytes == right.size_bytes
        )

    def _remember_detail_reference(self, reference: pb.ActivityDetailRefV2) -> None:
        if (
            not reference.run_id
            or not reference.scope_ref.reference
            or not reference.activity_id
            or not reference.object_uri
            or not reference.object_key
            or len(reference.sha256) != 64
            or reference.size_bytes > self._max_detail_body_bytes
        ):
            raise _DetailHydrationRaceError("activity detail reference is incomplete")
        with self._state_lock:
            existing = self._detail_refs_by_key.get(reference.object_key)
            if existing is not None and not self._same_detail_reference(existing, reference):
                raise _DetailHydrationRaceError(
                    "activity detail reference binding was replaced"
                )
            self._detail_refs_by_key[reference.object_key] = self._copy_detail_reference(
                reference
            )

    def _remember_trace_reference(
        self,
        reference: pb.ActivityDetailRefV2,
        *,
        node_id: str,
        revision: int,
    ) -> None:
        self._remember_detail_reference(reference)
        if reference.activity_id != f"trace:{node_id}:{revision}":
            raise _DetailHydrationRaceError(
                "trace detail reference does not match its descriptor"
            )
        with self._state_lock:
            self._trace_detail_refs[(reference.run_id, node_id, revision)] = (
                self._copy_detail_reference(reference)
            )

    def _remember_snapshot_bindings(self, response: pb.RunSnapshotV2) -> None:
        run_id = response.summary.run_id
        self._remember_activity_continuation(
            response.log_continuation,
            run_id=run_id,
            node_id="",
        )
        for node in response.nodes:
            self._remember_activity_continuation(
                node.activity_continuation,
                run_id=run_id,
                node_id=node.node_id,
            )
            if node.trace is not None and node.trace.available:
                self._remember_trace_reference(
                    node.trace.detail_ref,
                    node_id=node.node_id,
                    revision=node.trace.revision,
                )

    def _remember_update_bindings(self, message: pb.RunStatusEnvelopeV2) -> None:
        if (
            message.cursor.stream != "operator-events"
            or message.cursor.source_sequence != message.source_sequence
            or not self._has_complete_lifecycle_cursor(message.cursor)
        ):
            raise _RunUpdateResetError("update cursor is not a complete event binding")
        payload = message.WhichOneof("payload")
        if payload == "run_created":
            created = message.run_created
            run_id = created.summary.run_id
            for node in created.nodes:
                self._remember_activity_continuation(
                    node.activity_continuation,
                    run_id=run_id,
                    node_id=node.node_id,
                )
                if node.trace.available:
                    self._remember_trace_reference(
                        node.trace.detail_ref,
                        node_id=node.node_id,
                        revision=node.trace.revision,
                    )
        elif payload == "activity_appended":
            activity = message.activity_appended.activity
            if activity.kind in {"log", "agent_event"}:
                self._remember_detail_reference(activity.detail_ref)
            elif activity.kind == "trace" and activity.trace.available:
                self._remember_trace_reference(
                    activity.trace.detail_ref,
                    node_id=activity.node_id,
                    revision=activity.trace.revision,
                )

    def _activity_continuation_for(
        self,
        token: str,
        *,
        run_id: str,
        node_id: str,
    ) -> pb.ContinuationRefV2:
        with self._state_lock:
            continuation = self._activity_continuations.get((run_id, node_id, token))
        if continuation is None:
            raise _DetailHydrationRaceError("activity continuation is not server-issued")
        expected_stream = f"activity:{run_id}:{node_id or 'logs'}"
        if (
            continuation.scope_ref.reference == ""
            or continuation.cursor.stream != expected_stream
            or not self._has_complete_lifecycle_cursor(continuation.cursor)
        ):
            raise _DetailHydrationRaceError("activity continuation does not bind its target")
        return self._copy_continuation(continuation)

    def _detail_reference_for(
        self,
        body_token: str,
        size_bytes: int,
        *,
        scope: str | None,
    ) -> pb.ActivityDetailRefV2:
        with self._state_lock:
            reference = self._detail_refs_by_key.get(body_token)
        if reference is None:
            raise _DetailHydrationRaceError("activity detail reference is not server-issued")
        if reference.size_bytes != size_bytes:
            raise _DetailHydrationRaceError("activity detail reference size changed")
        if scope is not None and reference.scope_ref.reference != scope:
            raise _DetailHydrationRaceError("activity detail reference scope changed")
        return self._copy_detail_reference(reference)

    def _new_detail_budget(self) -> _DetailBudget:
        return _DetailBudget(
            max_count=self._max_retained_detail_count,
            max_bytes=self._max_retained_detail_bytes,
        )

    @staticmethod
    def _log_cache_key(run_id: str) -> _DetailCacheKey:
        return ("logs", run_id, "")

    @staticmethod
    def _agent_cache_key(run_id: str, node_id: str) -> _DetailCacheKey:
        return ("events", run_id, node_id)

    @staticmethod
    def _trace_cache_key(run_id: str, node_id: str) -> _DetailCacheKey:
        return ("trace", run_id, node_id)

    def _touch_detail_cache_locked(self, key: _DetailCacheKey) -> None:
        if key in self._detail_cache_usage:
            self._detail_cache_usage.move_to_end(key)

    def _evict_detail_cache_locked(self, key: _DetailCacheKey) -> None:
        usage = self._detail_cache_usage.pop(key, None)
        if usage is not None:
            self._retained_detail_count -= usage[0]
            self._retained_detail_bytes -= usage[1]
        kind, run_id, node_id = key
        if kind == "logs":
            self._log_entries.pop(run_id, None)
            self._log_sequences.pop(run_id, None)
            self._hydrated_log_runs.discard(run_id)
        elif kind == "events":
            node_key = (run_id, node_id)
            self._agent_events.pop(node_key, None)
            self._agent_event_sequences.pop(node_key, None)
            self._hydrated_agent_nodes.discard(node_key)
        else:
            node_key = (run_id, node_id)
            self._trace_bodies.pop(node_key, None)
            self._hydrated_trace_revisions.pop(node_key, None)

    def _reserve_detail_cache_locked(
        self,
        replacements: Mapping[_DetailCacheKey, tuple[int, int]],
        removals: set[_DetailCacheKey] | None = None,
    ) -> None:
        removed_keys = removals or set()
        replacement_count = sum(usage[0] for usage in replacements.values())
        replacement_bytes = sum(usage[1] for usage in replacements.values())
        if replacement_count > self._max_retained_detail_count:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "client detail hydration exceeds the configured retained body count limit",
            )
        if replacement_bytes > self._max_retained_detail_bytes:
            raise _ClientBudgetExceededError(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "client detail hydration exceeds the configured retained byte limit",
            )

        affected_keys = replacements.keys() | removed_keys
        replaced_count = sum(
            self._detail_cache_usage.get(key, (0, 0))[0] for key in affected_keys
        )
        replaced_bytes = sum(
            self._detail_cache_usage.get(key, (0, 0))[1] for key in affected_keys
        )
        retained_count = self._retained_detail_count - replaced_count
        retained_bytes = self._retained_detail_bytes - replaced_bytes
        retained_keys = len(self._detail_cache_usage.keys() - affected_keys)
        while (
            retained_count + replacement_count > self._max_retained_detail_count
            or retained_bytes + replacement_bytes > self._max_retained_detail_bytes
            or retained_keys + len(replacements) > self._max_retained_detail_count
        ):
            candidate = next(
                (key for key in self._detail_cache_usage if key not in affected_keys),
                None,
            )
            if candidate is None:
                raise RuntimeError("retained detail cache accounting is inconsistent")
            usage = self._detail_cache_usage[candidate]
            self._evict_detail_cache_locked(candidate)
            retained_count -= usage[0]
            retained_bytes -= usage[1]
            retained_keys -= 1

        for key in removed_keys:
            self._evict_detail_cache_locked(key)
        for key, usage in replacements.items():
            previous = self._detail_cache_usage.pop(key, None)
            if previous is not None:
                self._retained_detail_count -= previous[0]
                self._retained_detail_bytes -= previous[1]
            self._detail_cache_usage[key] = usage
            self._retained_detail_count += usage[0]
            self._retained_detail_bytes += usage[1]

    def _clear_detail_caches_locked(self) -> None:
        self._activity_continuations.clear()
        self._detail_refs_by_key.clear()
        self._trace_detail_refs.clear()
        self._log_sequences.clear()
        self._hydrated_log_runs.clear()
        self._log_entries.clear()
        self._agent_event_sequences.clear()
        self._hydrated_agent_nodes.clear()
        self._agent_events.clear()
        self._trace_bodies.clear()
        self._hydrated_trace_revisions.clear()
        self._detail_cache_usage.clear()
        self._retained_detail_count = 0
        self._retained_detail_bytes = 0

    def _evict_run_detail_caches_locked(self, run_id: str) -> None:
        for token, continuation in tuple(self._activity_continuations.items()):
            if continuation.cursor.stream.startswith(f"activity:{run_id}:"):
                del self._activity_continuations[token]
        for object_key, reference in tuple(self._detail_refs_by_key.items()):
            if reference.run_id == run_id:
                del self._detail_refs_by_key[object_key]
        for key in tuple(self._trace_detail_refs):
            if key[0] == run_id:
                del self._trace_detail_refs[key]
        cache_keys = {key for key in self._detail_cache_usage if key[1] == run_id}
        if run_id in self._log_entries or run_id in self._hydrated_log_runs:
            cache_keys.add(self._log_cache_key(run_id))
        cache_keys.update(
            self._agent_cache_key(*key)
            for key in self._agent_events.keys() | self._hydrated_agent_nodes
            if key[0] == run_id
        )
        cache_keys.update(
            self._trace_cache_key(*key) for key in self._trace_bodies if key[0] == run_id
        )
        cache_keys.update(
            self._trace_cache_key(*key)
            for key in self._hydrated_trace_revisions
            if key[0] == run_id
        )
        for key in cache_keys:
            self._evict_detail_cache_locked(key)

    @staticmethod
    def _cancel_detail_stream(stream: Any) -> None:
        cancel = getattr(stream, "cancel", None)
        if callable(cancel):
            cancel()

    def _read_detail_body(
        self, body_token: str, size_bytes: int, *, scope: str | None = None
    ) -> bytes:
        self._validate_detail_body_size(size_bytes)
        detail_ref = self._detail_reference_for(body_token, size_bytes, scope=scope)
        try:
            chunks = self._stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(detail_ref=detail_ref),
                **self._detail_rpc_kwargs(),
            )
            data = bytearray()
            saw_eof = False
            for expected_index, chunk in enumerate(chunks):
                if saw_eof:
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("detail stream continued after eof")
                if chunk.chunk_index != expected_index:
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("detail chunk identity changed")
                if len(chunk.data) > size_bytes - len(data):
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("detail body exceeded its advertised size")
                data.extend(chunk.data)
                saw_eof = chunk.eof
        except grpc.RpcError as error:
            raise self._record_unary_error(error) from error
        self._record_unary_success()
        if not saw_eof or len(data) != size_bytes:
            raise _DetailHydrationRaceError("detail body does not match its descriptor")
        resolved = bytes(data)
        if hashlib.sha256(resolved).hexdigest() != detail_ref.sha256:
            raise _DetailHydrationRaceError("detail body digest does not match its descriptor")
        return resolved

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
        expected_stream: str,
    ) -> int:
        if response.scope_ref.reference != operator_instance_id:
            raise _DetailHydrationRaceError("operator epoch changed during hydration")
        if (
            response.cursor.stream != expected_stream
            or not GrpcStateProvider._has_complete_lifecycle_cursor(response.cursor)
        ):
            raise _DetailHydrationRaceError("detail page cursor is not a complete binding")
        if response.cursor.source_sequence != expected_as_of:
            raise _DetailHydrationRaceError("detail high-water changed during hydration")
        return response.cursor.source_sequence

    def _commit_hydrated_run(
        self,
        snapshot: RunSnapshot,
        hydrated: RunState,
        *,
        starting_cursor: _StreamCursor,
        logs: list[LogEntry],
        log_bytes: int,
        hydrated_agent_nodes: set[tuple[str, str]],
        agent_sequences: dict[tuple[str, str], int],
        agent_events: dict[tuple[str, str], list[Any]],
        agent_event_bytes: dict[tuple[str, str], int],
        trace_bodies: dict[tuple[str, str], dict[str, Any]],
        trace_body_bytes: dict[tuple[str, str], int],
    ) -> RunState:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("state provider closed during detail hydration")
            current_cursor = self._cursor
            if (
                starting_cursor.operator_instance_id
                and starting_cursor.operator_instance_id != snapshot.operator_instance_id
            ):
                raise _DetailHydrationRaceError("snapshot epoch does not match client epoch")
            if (
                current_cursor.operator_instance_id
                and current_cursor.operator_instance_id != snapshot.operator_instance_id
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
                        hydrated_descriptor.revision if hydrated_descriptor is not None else 0
                    )
                    if current_trace_revision != hydrated_trace_revision:
                        raise _DetailHydrationRaceError(
                            f"trace descriptor advanced during hydration for {node_id}"
                        )
                    key = (hydrated.run_id, node_id)
                    if self._agent_event_sequences.get(key, 0) > agent_sequences.get(key, 0):
                        raise _DetailHydrationRaceError(
                            f"agent events advanced during hydration for {node_id}"
                        )
                    if (
                        descriptor is not None
                        and hydrated_node is not None
                        and descriptor.event_count
                        > len(agent_events.get((hydrated.run_id, node_id), ()))
                    ):
                        raise _DetailHydrationRaceError(
                            f"agent events advanced during hydration for {node_id}"
                        )
                structural = current if current.revision > hydrated.revision else hydrated
                result = deepcopy(structural)
            else:
                result = hydrated
            result.logs = []
            result.details_hydrated = False
            for node in result.nodes.values():
                node.agent_trace_json = None

            replacements: dict[_DetailCacheKey, tuple[int, int]] = {
                self._log_cache_key(result.run_id): (len(logs), log_bytes)
            }
            trace_revisions: dict[tuple[str, str], int] = {}
            for key in hydrated_agent_nodes:
                replacements[self._agent_cache_key(*key)] = (
                    len(agent_events.get(key, ())),
                    agent_event_bytes.get(key, 0),
                )
                if key in trace_bodies:
                    descriptor = result.nodes[key[1]].trace
                    if descriptor is None:
                        raise RuntimeError("hydrated trace body has no descriptor")
                    trace_revisions[key] = descriptor.revision
                    replacements[self._trace_cache_key(*key)] = (
                        1,
                        trace_body_bytes[key],
                    )
            retained_run_keys = {
                key for key in self._detail_cache_usage if key[1] == result.run_id
            }
            removed_cache_keys = retained_run_keys - replacements.keys()
            self._reserve_detail_cache_locked(
                replacements,
                removals=removed_cache_keys,
            )

            self._runs_by_id[result.run_id] = result
            self._run_revisions[result.run_id] = max(
                self._run_revisions.get(result.run_id, 0), result.revision
            )
            for node_id, node in result.nodes.items():
                key = (result.run_id, node_id)
                self._node_revisions[key] = max(self._node_revisions.get(key, 0), node.revision)
                if node.trace is not None:
                    self._trace_revisions[key] = max(
                        self._trace_revisions.get(key, 0), node.trace.revision
                    )
            self._log_entries[result.run_id] = logs
            self._hydrated_log_runs.add(result.run_id)
            self._log_sequences[result.run_id] = hydrated.latest_log_sequence
            for key in hydrated_agent_nodes:
                self._hydrated_agent_nodes.add(key)
                self._agent_event_sequences[key] = agent_sequences.get(key, 0)
                self._agent_events[key] = agent_events.get(key, [])
                if key in trace_bodies:
                    self._trace_bodies[key] = trace_bodies[key]
                    self._hydrated_trace_revisions[key] = trace_revisions[key]
                else:
                    self._trace_bodies.pop(key, None)
                    self._hydrated_trace_revisions.pop(key, None)
            return self._materialize_run_locked(result)

    def _materialize_run_locked(self, run: RunState) -> RunState:
        """Build compatibility detail only for an explicit state read."""
        result = deepcopy(run)
        if result.run_id in self._hydrated_log_runs:
            self._touch_detail_cache_locked(self._log_cache_key(result.run_id))
            result.logs = list(self._log_entries.get(result.run_id, ()))
            result.latest_log_sequence = self._log_sequences.get(result.run_id, 0)
            result.details_hydrated = True
        for node_id, node in result.nodes.items():
            key = (result.run_id, node_id)
            trace_body = self._trace_bodies.get(key)
            if key not in self._hydrated_agent_nodes and trace_body is None:
                node.agent_trace_json = None
                continue
            self._touch_detail_cache_locked(self._agent_cache_key(*key))
            self._touch_detail_cache_locked(self._trace_cache_key(*key))
            descriptor = node.trace
            status = descriptor.status if descriptor is not None else "in_progress"
            node.agent_trace_json = _materialize_agent_trace_json(
                self._agent_events.get(key, ()),
                status=status,
                trace_body=trace_body,
            )
        return result

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
                self._touch_detail_cache_locked(self._trace_cache_key(*key))
                current = self._runs_by_id.get(run_id)
                return self._materialize_run_locked(current) if current is not None else run

        self._validate_detail_body_size(descriptor.size_bytes)
        budget = self._new_detail_budget()
        budget.reserve_cache_key()
        budget.reserve(1, descriptor.size_bytes)
        with self._state_lock:
            stored_detail_ref = self._trace_detail_refs.get(
                (run_id, node_id, descriptor.revision)
            )
            detail_ref = (
                self._copy_detail_reference(stored_detail_ref)
                if stored_detail_ref is not None
                else None
            )
        if detail_ref is None:
            raise _DetailHydrationRaceError("trace detail reference is not server-issued")
        if (
            detail_ref.run_id != run_id
            or detail_ref.scope_ref.reference != run.operator_instance_id
            or detail_ref.size_bytes != descriptor.size_bytes
        ):
            raise _DetailHydrationRaceError("trace detail reference changed during hydration")
        try:
            chunks = self._stub.ReadActivityDetail(
                pb.ReadActivityDetailRequestV2(
                    detail_ref=self._copy_detail_reference(detail_ref)
                ),
                **self._detail_rpc_kwargs(),
            )
            data = bytearray()
            saw_eof = False
            for expected_index, chunk in enumerate(chunks):
                if saw_eof:
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("trace stream continued after eof")
                if chunk.chunk_index != expected_index:
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("trace chunk identity changed")
                if len(chunk.data) > descriptor.size_bytes - len(data):
                    self._cancel_detail_stream(chunks)
                    raise _DetailHydrationRaceError("trace body exceeded its advertised size")
                data.extend(chunk.data)
                saw_eof = chunk.eof
        except grpc.RpcError as error:
            raise self._record_unary_error(error) from error
        self._record_unary_success()
        if not saw_eof or len(data) != descriptor.size_bytes:
            raise _DetailHydrationRaceError("trace body does not match its descriptor")
        if hashlib.sha256(bytes(data)).hexdigest() != detail_ref.sha256:
            raise _DetailHydrationRaceError("trace body digest does not match its descriptor")
        try:
            trace = json.loads(data)
        except (TypeError, ValueError) as error:
            raise _DetailHydrationRaceError("trace body is not valid JSON") from error
        if not isinstance(trace, dict):
            raise _DetailHydrationRaceError("trace body is not a JSON object")

        with self._state_lock:
            if self._closed:
                return None
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
                    and current_cursor.operator_instance_id != run.operator_instance_id
                )
                or current_descriptor is None
                or current_descriptor.revision != descriptor.revision
            ):
                return None
            self._reserve_detail_cache_locked(
                {
                    self._trace_cache_key(*key): (1, descriptor.size_bytes),
                }
            )
            self._trace_bodies[key] = trace
            self._hydrated_trace_revisions[key] = descriptor.revision
            return self._materialize_run_locked(current)

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
        request = pb.StartRunRequestV2(
            workflow_selector=workflow_selector,
            run_id=run_id or f"run_{uuid4().hex[:8]}",
            input_json=_json_payload(input),
            context_json=_json_payload(context),
            input_files=input_files,
        )
        resp = self._call(self._stub.StartRun, request)
        return resp.run_id

    def cancel_run(self, run_id: str) -> None:
        self._call(self._stub.CancelRun, pb.CancelRunRequestV2(run_id=run_id))

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._run_callbacks.append(callback)

    def on_catalog_update(self, callback: Callable[[CatalogSnapshot], None]) -> None:
        self._catalog_callbacks.append(callback)

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._log_callbacks.append(callback)

    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None:
        self._detail_callbacks.append(callback)

    def start_stream(self) -> None:
        """Start update consumption after every callback is registered."""
        self._ensure_stream()

    def on_stream_reset(self, callback: Callable[[StreamResetNotice], None]) -> None:
        self._stream_reset_callbacks.append(callback)

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline:
        """Load one authoritative structural baseline for an exact reset."""
        loader = self._reset_baseline_loader
        if loader is not None:
            baseline = loader(notice)
            self._validate_reset_baseline(notice, baseline)
            self._remember_validated_reset_baseline(notice, baseline)
            return baseline

        last_error: Exception | None = None
        for attempt in range(RESET_BASELINE_MAX_ATTEMPTS):
            try:
                baseline = self._load_authoritative_reset_baseline(notice)
                self._validate_reset_baseline(notice, baseline)
                self._remember_validated_reset_baseline(notice, baseline)
                return baseline
            except _ResetBaselineMismatchError as error:
                last_error = error
            except _ClientBudgetExceededError:
                raise
            except OperatorCallError as error:
                if error.status not in _RESET_BASELINE_RETRY_STATUSES:
                    raise
                last_error = error

            if attempt + 1 < RESET_BASELINE_MAX_ATTEMPTS:
                if self._stream_stop.wait(RESET_BASELINE_RETRY_SECONDS):
                    raise RuntimeError("state provider closed during baseline loading")

        raise RuntimeError(
            "operator state did not stabilize while loading the reset baseline"
        ) from last_error

    def _load_authoritative_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline:
        catalog = self.get_catalog()
        marker, summaries = self._list_run_summaries()
        snapshots = [
            self._get_consistent_run_snapshot(summary, marker) for summary in summaries
        ]
        confirmed_catalog = self.get_catalog()
        if replace(confirmed_catalog, as_of_sequence=0) != replace(catalog, as_of_sequence=0):
            raise _ResetBaselineMismatchError(
                "workflow catalog changed during baseline loading"
            )
        if (
            catalog.operator_instance_id != marker[0]
            or confirmed_catalog.operator_instance_id != marker[0]
            or catalog.as_of_sequence > marker[1]
        ):
            raise _ResetBaselineMismatchError(
                "workflow catalog does not span the run baseline high-water mark"
            )

        runs_by_workflow = self._group_snapshot_runs(catalog.workflows, snapshots)
        return ResetBaseline(
            generation=notice.generation,
            operator_instance_id=marker[0],
            as_of_sequence=marker[1],
            catalog=catalog,
            runs_by_workflow=runs_by_workflow,
        )

    def _list_run_summaries(
        self,
    ) -> tuple[tuple[str, int], list[RunSummary]]:
        summaries: list[RunSummary] = []
        page_token: pb.ContinuationRefV2 | None = None
        seen_tokens: set[str] = set()
        seen_run_ids: set[str] = set()
        marker: tuple[str, int] | None = None
        page_count = 0
        while True:
            page_count += 1
            self._validate_page_accumulation(page_count, "run summary pages")
            request = pb.ListRunSummariesRequestV2(page_size=RESET_BASELINE_PAGE_SIZE)
            if page_token is not None:
                request.continuation.CopyFrom(page_token)
            page = self._call(self._stub.ListRunSummaries, request)
            page_marker = (page.scope_ref.reference, page.cursor.source_sequence)
            if marker is None:
                if not page_marker[0]:
                    raise _ResetBaselineMismatchError(
                        "operator summary page omitted its instance identifier"
                    )
                marker = page_marker
            elif page_marker != marker:
                raise _ResetBaselineMismatchError(
                    "run summary pages crossed an operator epoch or high-water"
                )
            assert marker is not None
            for message in page.runs:
                self._validate_page_accumulation(
                    len(summaries) + 1,
                    "run summaries",
                )
                summary = run_summary_from_v2(message)
                if summary.run_id in seen_run_ids:
                    raise _ResetBaselineMismatchError(
                        f"run summary {summary.run_id!r} appeared on multiple pages"
                    )
                seen_run_ids.add(summary.run_id)
                summaries.append(summary)
            next_page = page.next_page
            if not next_page.continuation_id:
                return marker, summaries
            if next_page.continuation_id in seen_tokens:
                raise _ResetBaselineMismatchError(
                    "run summary pagination repeated a page token"
                )
            seen_tokens.add(next_page.continuation_id)
            page_token = next_page

    def _get_consistent_run_snapshot(
        self,
        summary: RunSummary,
        marker: tuple[str, int],
    ) -> RunSnapshot:
        message = self._call(
            self._stub.GetRunSnapshot,
            pb.GetRunSnapshotRequestV2(run_id=summary.run_id),
        )
        self._remember_snapshot_bindings(message)
        snapshot = run_snapshot_from_v2(message)
        if snapshot.operator_instance_id != marker[0]:
            raise _ResetBaselineMismatchError(
                f"run snapshot {summary.run_id!r} crossed the operator epoch"
            )
        if snapshot.summary != summary:
            raise _ResetBaselineMismatchError(
                f"run snapshot {summary.run_id!r} changed after summary pagination"
            )
        return snapshot

    @staticmethod
    def _group_snapshot_runs(
        workflows: tuple[WorkflowInfo, ...],
        snapshots: list[RunSnapshot],
    ) -> dict[str, tuple[RunState, ...]]:
        runs_by_workflow: dict[str, list[RunState]] = {
            workflow.selector: [] for workflow in workflows
        }
        selectors_by_name: dict[str, list[str]] = {}
        for workflow in workflows:
            selectors_by_name.setdefault(workflow.name, []).append(workflow.selector)

        for snapshot in sorted(
            snapshots,
            key=lambda item: (
                item.summary.created_sequence,
                item.summary.run_id,
            ),
        ):
            selector = snapshot.summary.workflow_id
            if not selector:
                candidates = selectors_by_name.get(snapshot.summary.flow_name, [])
                selector = candidates[0] if len(candidates) == 1 else snapshot.summary.flow_name
            if selector not in runs_by_workflow:
                runs_by_workflow[selector] = []
            runs_by_workflow[selector].append(_run_from_snapshot(snapshot))

        return {selector: tuple(runs) for selector, runs in runs_by_workflow.items()}

    @staticmethod
    def _validate_reset_baseline(
        notice: StreamResetNotice,
        baseline: ResetBaseline,
    ) -> None:
        if baseline.generation != notice.generation:
            raise _ResetBaselineMismatchError(
                "reset baseline generation does not match the pending reset"
            )
        if not baseline.operator_instance_id:
            raise _ResetBaselineMismatchError(
                "reset baseline omitted its operator instance identifier"
            )
        if (
            notice.operator_instance_id
            and baseline.operator_instance_id != notice.operator_instance_id
        ):
            raise _ResetBaselineMismatchError(
                "reset baseline operator instance does not match the pending reset"
            )
        if baseline.as_of_sequence < notice.observed_sequence:
            raise _ResetBaselineMismatchError(
                "reset baseline precedes the observed reset sequence"
            )

    def _remember_validated_reset_baseline(
        self,
        notice: StreamResetNotice,
        baseline: ResetBaseline,
    ) -> None:
        """Bind a validated baseline to the reset that is currently pending."""
        with self._lifecycle_lock:
            pending = self._pending_reset
            if (
                pending is not None
                and pending.generation == notice.generation
                and self.stream_state is StreamState.RESET_REQUIRED
            ):
                self._validated_reset_baseline = baseline

    def acknowledge_stream_reset(
        self,
        generation: int,
        operator_instance_id: str,
        reconciled_sequence: int,
    ) -> None:
        """Acknowledge the exact reset generation after installing its baseline."""
        if not operator_instance_id:
            raise ValueError("operator_instance_id must not be empty")
        if (
            isinstance(reconciled_sequence, bool)
            or not isinstance(reconciled_sequence, int)
            or reconciled_sequence < 0
        ):
            raise ValueError("reconciled_sequence must be a non-negative integer")
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("state provider is closed")
            pending = self._pending_reset
            if (
                pending is None
                or pending.generation != generation
                or self.stream_state is not StreamState.RESET_REQUIRED
            ):
                raise StaleResetAcknowledgementError(
                    f"reset generation {generation} is no longer pending"
                )
            validated = self._validated_reset_baseline
            if validated is None or validated.generation != generation:
                raise StaleResetAcknowledgementError(
                    f"reset generation {generation} has no validated baseline"
                )
            expected = (
                validated.generation,
                validated.operator_instance_id,
                validated.as_of_sequence,
            )
            acknowledged = (
                generation,
                operator_instance_id,
                reconciled_sequence,
            )
            if acknowledged != expected:
                raise ValueError("reset acknowledgement does not match the validated baseline")
            runs = {
                run.run_id: run
                for workflow_runs in validated.runs_by_workflow.values()
                for run in workflow_runs
            }
            with self._state_lock:
                self._install_catalog_locked(validated.catalog)
            self._replace_structural_baseline(
                operator_instance_id,
                reconciled_sequence,
                runs,
            )
            if self._pending_event_cursor is not None:
                with self._state_lock:
                    self._event_cursor = self._copy_lifecycle_cursor(
                        self._pending_event_cursor
                    )
            self._pending_reset = None
            self._pending_event_cursor = None
            self._validated_reset_baseline = None
            self.stream_state = StreamState.LIVE
            self.stream_retry_count = 0
            self.stream_error = ""
            self._reset_acknowledged.set()
        self._notify_catalog_callbacks(validated.catalog)

    def _ensure_stream(self) -> None:
        """Start the background streaming thread if not already running."""
        with self._lifecycle_lock:
            if self._closed or self._stream_stop.is_set():
                return
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return
            if self.stream_state is not StreamState.RESET_REQUIRED:
                self.stream_state = StreamState.CONNECTING
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
            self._stream_thread.start()

    def ping(self) -> bool:
        """Quick health check — try a fast unary call with short timeout."""
        try:
            kwargs = {"timeout": min(2.0, self._unary_timeout)}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            self._stub.DiscoverFlows(pb.DiscoverFlowsRequestV2(page_size=1), **kwargs)
            self._record_unary_success()
            return True
        except grpc.RpcError as e:
            self._record_unary_error(e)
            return False

    def _install_catalog_locked(self, catalog: CatalogSnapshot) -> None:
        if (
            catalog.operator_instance_id == self._catalog.operator_instance_id
            and catalog.revision < self._catalog.revision
        ):
            return
        self._catalog = deepcopy(catalog)
        self.discovery_diagnostics = list(catalog.diagnostics)

    def _stream_loop(self) -> None:
        """Consume ordered operator updates without conflating stream and unary health."""
        while not self._stream_stop.is_set():
            try:
                with self._lifecycle_lock:
                    if self._closed:
                        break
                    if self.stream_state is not StreamState.RESET_REQUIRED:
                        self.stream_state = StreamState.CONNECTING
                    self.stream_retry_count += 1
                with self._state_lock:
                    event_cursor = (
                        self._copy_lifecycle_cursor(self._event_cursor)
                        if self._event_cursor is not None
                        else None
                    )
                request = pb.WatchRunStatusRequestV2()
                if event_cursor is not None:
                    request.after_cursor.CopyFrom(event_cursor)
                stream = self._stub.WatchRunStatus(
                    request,
                    metadata=self._metadata,
                )
                with self._lifecycle_lock:
                    if self._closed:
                        break
                    if self.stream_state is not StreamState.RESET_REQUIRED:
                        self.stream_state = StreamState.REPLAYING

                initial_metadata = getattr(stream, "initial_metadata", None)
                if callable(initial_metadata):
                    initial_metadata()
                with self._lifecycle_lock:
                    if self._closed:
                        break
                    self.operator_reachable = True
                    if self.stream_state is not StreamState.RESET_REQUIRED:
                        self.stream_state = StreamState.LIVE
                    self.stream_error = ""

                reconnect = False
                for message in stream:
                    if self._stream_stop.is_set():
                        break
                    self._remember_update_bindings(message)
                    envelope = operator_update_envelope_from_v2(message)
                    if not envelope.operator_instance_id:
                        raise RuntimeError(
                            "update envelope omitted its operator instance identifier"
                        )

                    with self._state_lock:
                        current_cursor = self._cursor
                    reset = envelope.reset_required
                    epoch_changed = (
                        bool(current_cursor.operator_instance_id)
                        and envelope.operator_instance_id != current_cursor.operator_instance_id
                    )
                    if reset is not None or epoch_changed:
                        observed_sequence = (
                            reset.latest_sequence
                            if reset is not None
                            else envelope.update.sequence
                        )
                        self._require_stream_reset(
                            envelope.operator_instance_id,
                            observed_sequence,
                            event_cursor=message.cursor,
                        )
                        reconnect = True
                        break

                    try:
                        run, detail = self._apply_update_envelope(
                            envelope,
                            event_cursor=message.cursor,
                        )
                    except _RunUpdateResetError:
                        assert envelope.update is not None
                        self._require_stream_reset(
                            envelope.operator_instance_id,
                            envelope.update.sequence,
                            event_cursor=message.cursor,
                        )
                        reconnect = True
                        break
                    with self._state_lock:
                        made_progress = self._cursor != current_cursor
                    if run is not None:
                        self._notify_run_callbacks(run)
                    if detail is not None:
                        self._notify_detail_callbacks(detail)
                        if isinstance(detail, LogDetailAppended):
                            self._notify_log_callbacks(detail.log)
                    with self._lifecycle_lock:
                        if self._closed:
                            break
                        self.operator_reachable = True
                        if self.stream_state is not StreamState.RESET_REQUIRED:
                            self.stream_state = StreamState.LIVE
                        if made_progress:
                            self.stream_retry_count = 0
                        self.stream_error = ""

                if reconnect:
                    continue
                if self._stream_stop.is_set():
                    break
                raise RuntimeError("operator update stream ended")
            except grpc.RpcError as error:
                if self._stream_stop.is_set():
                    break
                with self._lifecycle_lock:
                    if self.stream_state is not StreamState.RESET_REQUIRED:
                        self.stream_state = StreamState.FAILED
                    self.stream_error = f"{error.code().name}: {error.details()}"
                    retry_count = self.stream_retry_count
                delay = min(2 ** min(retry_count, 5), 30)
                self._stream_stop.wait(delay)
            except Exception as error:
                if self._stream_stop.is_set():
                    break
                with self._lifecycle_lock:
                    if self.stream_state is not StreamState.RESET_REQUIRED:
                        self.stream_state = StreamState.FAILED
                    self.stream_error = str(error)
                self._stream_stop.wait(2.0)

        with self._lifecycle_lock:
            self.stream_state = StreamState.STOPPED

    def _require_stream_reset(
        self,
        operator_instance_id: str,
        observed_sequence: int,
        *,
        event_cursor: pb.LifecycleCursorV2,
    ) -> None:
        """Block update consumption until the exact replacement baseline is installed."""
        if (
            event_cursor.stream != "operator-events"
            or event_cursor.source_sequence != observed_sequence
            or not self._has_complete_lifecycle_cursor(event_cursor)
        ):
            raise _RunUpdateResetError("reset cursor is not a complete event binding")
        with self._lifecycle_lock:
            self._reset_generation += 1
            notice = StreamResetNotice(
                generation=self._reset_generation,
                previous_sequence=self._cursor.sequence,
                observed_sequence=observed_sequence,
                operator_instance_id=operator_instance_id,
            )
            self._pending_reset = notice
            self._pending_event_cursor = self._copy_lifecycle_cursor(event_cursor)
            self._validated_reset_baseline = None
            self.stream_state = StreamState.RESET_REQUIRED
            self._reset_acknowledged.clear()

        for callback in tuple(self._stream_reset_callbacks):
            try:
                callback(notice)
            except Exception:
                pass
        while not self._stream_stop.is_set():
            if self._reset_acknowledged.wait(0.1):
                break

    def _apply_update_envelope(
        self,
        envelope: OperatorUpdateEnvelope,
        *,
        event_cursor: pb.LifecycleCursorV2 | None = None,
    ) -> tuple[RunState | None, DetailUpdate | None]:
        update = envelope.update
        with self._state_lock:
            if update is not None and update.sequence <= self._cursor.sequence:
                return None, None
        log_detail: LogEntry | None = None
        event_detail: AgentEvent | None = None
        if update is not None and isinstance(update.change, LogAppended):
            descriptor = update.change.log
            message = self._read_detail_body(
                descriptor.body_token,
                descriptor.size_bytes,
            ).decode()
            log_detail = LogEntry(
                timestamp=descriptor.timestamp,
                level=descriptor.level,
                node_id=descriptor.node_id,
                message=message,
            )
        elif update is not None and isinstance(update.change, AgentEventAppended):
            descriptor = update.change.event
            event_detail = AgentEvent(
                invocation_id=descriptor.invocation_id,
                event_sequence=descriptor.event_sequence,
                event_json=self._read_detail_body(
                    descriptor.body_token,
                    descriptor.size_bytes,
                ).decode(),
                event_kind=descriptor.event_kind,
                iteration=descriptor.iteration,
                duration_ms=descriptor.duration_ms,
                error=descriptor.error,
                tool_count=descriptor.tool_count,
                predict_count=descriptor.predict_count,
                size_bytes=descriptor.size_bytes,
            )
        with self._state_lock:
            result = self._apply_update_envelope_locked(
                envelope,
                log_detail=log_detail,
                event_detail=event_detail,
                event_cursor=event_cursor,
            )
            catalog = (
                deepcopy(self._catalog)
                if update is not None and isinstance(update.change, CatalogReplaced)
                else None
            )
        if catalog is not None:
            self._notify_catalog_callbacks(catalog)
        return result

    def _apply_update_envelope_locked(
        self,
        envelope: OperatorUpdateEnvelope,
        *,
        log_detail: LogEntry | None = None,
        event_detail: AgentEvent | None = None,
        event_cursor: pb.LifecycleCursorV2 | None = None,
    ) -> tuple[RunState | None, DetailUpdate | None]:
        update = envelope.update
        if self._closed:
            return None, None
        if update is None:
            raise _RunUpdateResetError("update payload missing")
        if event_cursor is not None and (
            event_cursor.stream != "operator-events"
            or event_cursor.source_sequence != update.sequence
            or not self._has_complete_lifecycle_cursor(event_cursor)
        ):
            raise _RunUpdateResetError("update cursor is not a complete event binding")
        cursor = self._cursor
        operator_instance_id = cursor.operator_instance_id
        if operator_instance_id:
            if envelope.operator_instance_id != operator_instance_id:
                raise _RunUpdateResetError("operator epoch changed")
        else:
            operator_instance_id = envelope.operator_instance_id

        if update.sequence <= cursor.sequence:
            return None, None
        if update.sequence != cursor.sequence + 1:
            raise _RunUpdateResetError("update sequence gap")

        change = update.change
        detail: DetailUpdate | None = None
        if isinstance(change, CatalogReplaced):
            catalog = change.catalog
            if (
                catalog.operator_instance_id != envelope.operator_instance_id
                or catalog.as_of_sequence != update.sequence
            ):
                raise _RunUpdateResetError("catalog update marker mismatch")
            self._install_catalog_locked(catalog)
            run = None
        elif isinstance(change, WorkflowReloadStatus):
            run = None
        elif isinstance(change, RunCreated):
            run = _run_from_created(envelope.operator_instance_id, change)
            old_revision = self._run_revisions.get(run.run_id, -1)
            if change.summary.revision <= old_revision:
                run = None
            else:
                self._evict_run_detail_caches_locked(run.run_id)
                self._reserve_detail_cache_locked({self._log_cache_key(run.run_id): (0, 0)})
                self._runs_by_id[run.run_id] = run
                self._run_revisions[run.run_id] = change.summary.revision
                for node in run.nodes.values():
                    self._node_revisions[(run.run_id, node.node_id)] = node.revision
                self._hydrated_log_runs.add(run.run_id)
                self._log_sequences[run.run_id] = 0
                self._log_entries[run.run_id] = []
        else:
            current = self._runs_by_id.get(change.run_id)
            if current is None:
                raise _RunUpdateResetError(f"update references unknown run {change.run_id}")
            run = replace(current)
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
                    raise _RunUpdateResetError(
                        f"update references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                if change.revision <= self._node_revisions.get(key, 0):
                    run = None
                else:
                    node = replace(node)
                    node.status = change.status
                    node.started_at = change.started_at
                    node.ended_at = change.ended_at
                    node.error = change.error
                    node.revision = change.revision
                    run.nodes = dict(current.nodes)
                    run.nodes[change.node_id] = node
                    self._node_revisions[key] = change.revision
            elif isinstance(change, LogAppended):
                if log_detail is None:
                    raise _RunUpdateResetError("log update detail is unavailable")
                logs_hydrated = change.run_id in self._hydrated_log_runs
                known_sequence = self._log_sequences.get(change.run_id, 0)
                if logs_hydrated and change.log.sequence <= known_sequence:
                    run = None
                else:
                    if logs_hydrated:
                        cache_key = self._log_cache_key(change.run_id)
                        retained_count, retained_bytes = self._detail_cache_usage.get(
                            cache_key,
                            (len(self._log_entries.get(change.run_id, ())), 0),
                        )
                        replacement = (
                            retained_count + 1,
                            retained_bytes + change.log.size_bytes,
                        )
                        if (
                            replacement[0] > self._max_retained_detail_count
                            or replacement[1] > self._max_retained_detail_bytes
                        ):
                            self._evict_detail_cache_locked(cache_key)
                        else:
                            self._reserve_detail_cache_locked({cache_key: replacement})
                            self._log_entries.setdefault(change.run_id, []).append(log_detail)
                            self._log_sequences[change.run_id] = change.log.sequence
                    run.latest_log_sequence = max(run.latest_log_sequence, change.log.sequence)
                    detail = LogDetailAppended(
                        operator_instance_id=operator_instance_id,
                        run_id=change.run_id,
                        created_sequence=current.created_sequence,
                        sequence=update.sequence,
                        log_sequence=change.log.sequence,
                        log=log_detail,
                    )
            elif isinstance(change, AgentEventAppended):
                if event_detail is None:
                    raise _RunUpdateResetError("agent event update detail is unavailable")
                node = run.nodes.get(change.node_id)
                if node is None:
                    raise _RunUpdateResetError(
                        f"update references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                node_hydrated = key in self._hydrated_agent_nodes
                known_sequence = self._agent_event_sequences.get(key, 0)
                if node_hydrated and change.event.event_sequence <= known_sequence:
                    run = None
                else:
                    node_events = self._agent_events.setdefault(key, [])
                    if node_hydrated or not node_events:
                        cache_key = self._agent_cache_key(*key)
                        retained_count, retained_bytes = self._detail_cache_usage.get(
                            cache_key,
                            (len(node_events), 0),
                        )
                        replacement = (
                            retained_count + 1,
                            retained_bytes + change.event.size_bytes,
                        )
                        if (
                            replacement[0] > self._max_retained_detail_count
                            or replacement[1] > self._max_retained_detail_bytes
                        ):
                            self._evict_detail_cache_locked(cache_key)
                        else:
                            self._reserve_detail_cache_locked({cache_key: replacement})
                            _append_agent_event(node_events, event_detail.event_json)
                            self._hydrated_agent_nodes.add(key)
                            self._agent_event_sequences[key] = change.event.event_sequence
                    detail = AgentEventDetailAppended(
                        operator_instance_id=operator_instance_id,
                        run_id=change.run_id,
                        created_sequence=current.created_sequence,
                        sequence=update.sequence,
                        node_id=change.node_id,
                        event=event_detail,
                    )
            elif isinstance(change, TraceFinalized):
                node = run.nodes.get(change.node_id)
                if node is None:
                    raise _RunUpdateResetError(
                        f"update references unknown node {change.run_id}/{change.node_id}"
                    )
                key = (change.run_id, change.node_id)
                if change.trace.revision <= self._trace_revisions.get(key, 0):
                    run = None
                else:
                    node = replace(node)
                    node.trace = change.trace
                    node.revision = max(node.revision, change.trace.revision)
                    run.nodes = dict(current.nodes)
                    run.nodes[change.node_id] = node
                    self._evict_detail_cache_locked(self._trace_cache_key(*key))
                    self._trace_revisions[key] = change.trace.revision
            else:
                raise _RunUpdateResetError("unsupported update change")

            if run is not None:
                run.operator_instance_id = operator_instance_id
                run.revision = max(run.revision, update.sequence)
                self._runs_by_id[run.run_id] = run

        self.operator_instance_id = operator_instance_id
        self._cursor = _StreamCursor(operator_instance_id, update.sequence)
        if event_cursor is not None:
            self._event_cursor = self._copy_lifecycle_cursor(event_cursor)
        return run, detail

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
        raise _RunUpdateResetError("authoritative structural baseline loader is not installed")

    def _install_structural_baseline(
        self,
        operator_instance_id: str,
        as_of_sequence: int,
        runs: dict[str, RunState],
    ) -> None:
        """Install one exact structural epoch and notify update consumers."""
        self._replace_structural_baseline(
            operator_instance_id,
            as_of_sequence,
            runs,
        )
        for run in runs.values():
            self._notify_run_callbacks(run)

    def _replace_structural_baseline(
        self,
        operator_instance_id: str,
        as_of_sequence: int,
        runs: dict[str, RunState],
    ) -> None:
        """Replace reducer state without publishing duplicate UI updates."""
        with self._state_lock:
            for run in runs.values():
                run.details_hydrated = False
            self._runs_by_id = dict(runs)
            self._run_revisions = {run_id: run.revision for run_id, run in runs.items()}
            self._node_revisions = {
                (run_id, node_id): node.revision
                for run_id, run in runs.items()
                for node_id, node in run.nodes.items()
            }
            self._clear_detail_caches_locked()
            self._trace_revisions = {
                (run_id, node_id): node.trace.revision
                for run_id, run in runs.items()
                for node_id, node in run.nodes.items()
                if node.trace is not None
            }
            self.operator_instance_id = operator_instance_id
            self._cursor = _StreamCursor(operator_instance_id, as_of_sequence)
            self._event_cursor = None

    def _notify_catalog_callbacks(self, catalog: CatalogSnapshot) -> None:
        for callback in self._catalog_callbacks:
            try:
                callback(deepcopy(catalog))
            except Exception:
                pass

    def _notify_run_callbacks(self, run: RunState) -> None:
        for callback in self._run_callbacks:
            try:
                callback(_structural_callback_projection(run))
            except Exception:
                pass

    def _notify_log_callbacks(self, log: LogEntry) -> None:
        for callback in self._log_callbacks:
            try:
                callback(replace(log))
            except Exception:
                pass

    def _notify_detail_callbacks(self, detail: DetailUpdate) -> None:
        for callback in self._detail_callbacks:
            try:
                callback(_detail_callback_projection(detail))
            except Exception:
                pass

    def close(self) -> None:
        """Stop stream reconnects and close the gRPC channel."""
        with self._lifecycle_lock:
            close_channel = not self._closed
            self._closed = True
            self._stream_stop.set()
            self._reset_acknowledged.set()
            self._validated_reset_baseline = None
            self.operator_reachable = False
            self.stream_state = StreamState.STOPPED
            thread = self._stream_thread
        if close_channel:
            self._channel.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STREAM_THREAD_JOIN_TIMEOUT_SECONDS)
        with self._state_lock:
            self._clear_detail_caches_locked()


def _structural_callback_projection(run: RunState) -> RunState:
    """Detach one body-free projection from reducer-owned mutable state."""
    return replace(
        run,
        nodes={
            node_id: replace(node, agent_trace_json=None) for node_id, node in run.nodes.items()
        },
        logs=[],
        details_hydrated=False,
    )


def _detail_callback_projection(detail: DetailUpdate) -> DetailUpdate:
    """Detach the one changed detail value without copying cumulative history."""
    if isinstance(detail, LogDetailAppended):
        return replace(detail, log=replace(detail.log))
    return replace(detail, event=replace(detail.event))


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
        triggered_at=summary.triggered_at,
        ended_at=summary.ended_at,
        triggered_by=summary.triggered_by,
        workflow_id=summary.workflow_id,
        workflow_display_name=summary.workflow_display_name,
        topology=created.topology,
        operator_instance_id=operator_instance_id,
        created_sequence=summary.created_sequence,
        revision=summary.revision,
        details_hydrated=False,
    )
    run.nodes = {
        item.node_id: NodeState(
            node_id=item.node_id,
            name=item.name,
            node_type=item.node_type,
            status=item.status,
            started_at=item.started_at,
            ended_at=item.ended_at,
            error=item.error,
            trace=item.trace,
            revision=item.revision,
            event_page_token=item.event_page_token,
        )
        for item in created.nodes
    }
    return run


def _run_from_snapshot(snapshot: RunSnapshot) -> RunState:
    """Materialize the authoritative structural baseline used by the reducer."""
    run = _run_from_created(
        snapshot.operator_instance_id,
        RunCreated(
            summary=snapshot.summary,
            nodes=snapshot.nodes,
            topology=snapshot.topology,
        ),
    )
    run.latest_log_sequence = snapshot.latest_log_sequence
    run.details_hydrated = False
    return run


def _trace_detail_from_run(run: RunState | None, node_id: str) -> TraceDetail | None:
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


def _append_agent_event(events: list[Any], event_json: str) -> None:
    try:
        event = json.loads(event_json)
    except json.JSONDecodeError:
        event = {"raw": event_json}
    events.append(event)


def _materialize_agent_trace_json(
    events: Sequence[Any],
    *,
    status: str,
    trace_body: dict[str, Any] | None,
) -> str:
    reconstructed = deepcopy(trace_body) if trace_body is not None else None
    if reconstructed is not None:
        steps = []
        evidence_events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_kind = event.get("event_kind")
            data = event.get("data")
            if event_kind == "iteration.recorded" and isinstance(data, dict):
                step = data.get("step")
                if isinstance(step, dict):
                    steps.append(step)
            evidence_events.append(
                {
                    "sequence": event.get("sequence"),
                    "kind": event_kind,
                    "timestamp_ns": event.get("timestamp_ns"),
                    "data": data if isinstance(data, dict) else {},
                }
            )
        reconstructed["steps"] = steps
        evidence = reconstructed.get("evidence")
        if isinstance(evidence, dict):
            evidence["events"] = evidence_events

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "run_id": None,
        "events": events,
        "trace": reconstructed,
        "error": None,
    }
    if reconstructed is not None:
        envelope["status"] = str(reconstructed.get("status") or status)
        evidence = reconstructed.get("evidence")
        if isinstance(evidence, dict):
            envelope["run_id"] = evidence.get("run_id")
    return json.dumps(envelope, default=str)


def _json_payload(payload: Mapping[str, Any] | BaseModel | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()
    return json.dumps(payload, default=_json_payload_default)


def _json_payload_default(value: Any) -> Any:
    if isinstance(value, Workspace):
        return value._manifest_for_serialization()
    raise TypeError(f"Input JSON does not support {type(value).__name__}")


def _file_attachment(field_name: str, value: File | bytes) -> pb.FileAttachmentV2:
    file = value if isinstance(value, File) else File(name=field_name, content=value)
    return pb.FileAttachmentV2(
        attachment_id=f"inline:{field_name}:{file.name or ''}",
        field_name=field_name,
        name=file.name or "",
        media_type=file.content_type or "",
        sha256=file.sha256 or "",
        size_bytes=len(file.content),
        inline_bytes=file.content,
    )


def _v2_continuation(scope: str, token: str) -> pb.ContinuationRefV2 | None:
    """Wrap one snapshot-issued page token in its scope-bound continuation."""
    if not token:
        return None
    return pb.ContinuationRefV2(
        scope_ref=pb.ScopeReferenceV2(reference=scope),
        continuation_id=token,
    )


def _validate_wire_result_payload(
    value_json: str,
    files: tuple[ResultFileAttachment, ...],
) -> None:
    value_size = len(value_json.encode("utf-8"))
    if value_size > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Workflow result JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    if len(files) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    total_attachment_bytes = 0
    for item in files:
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
