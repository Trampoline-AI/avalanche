"""Operator — isolated run coordination and parent-owned state."""

from __future__ import annotations

import base64
import json
import logging
import math
import multiprocessing
import os
import queue
import signal
import subprocess
import threading
import time
import warnings
from bisect import bisect_right
from collections import OrderedDict, deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Literal, TypeAlias
from uuid import uuid4

from ..executor import LocalExecutor, RayExecutor
from .discovery import DEFAULT_DISCOVERY_TIMEOUT
from .models import (
    AgentEvent,
    AgentEventAppended,
    AgentEventDescriptor,
    AgentEventDetailAppended,
    AgentEventPage,
    CatalogReloadRequired,
    CatalogReplaced,
    CatalogSnapshot,
    CatalogView,
    DetailUpdate,
    FinalizedTrace,
    LogAppended,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    LogPage,
    LogRecordDescriptor,
    NodeSnapshot,
    NodeState,
    NodeStatus,
    NodeStatusChanged,
    OperatorUpdate,
    OperatorUpdateChange,
    OperatorUpdateEnvelope,
    ResetRequired,
    RunCreated,
    RunSnapshot,
    RunState,
    RunStatus,
    RunStatusChanged,
    RunSummary,
    RunSummaryPage,
    SequencedLogEntry,
    TerminalSealAppended,
    TerminalSealDescriptor,
    TraceDescriptor,
    TraceFinalized,
    TraceHeader,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
    WorkflowReloadStatus,
    WorkflowTopology,
)
from .registry import AmbiguousWorkflow, WorkflowRegistry
from .result_store import (
    PendingResultBundle,
    ResultPublicationCancelledError,
    ResultStore,
    StoredWorkflowResult,
    duplicate_bundle_descriptor_for_spawn,
    require_worker_descriptor_transfer,
)
from .results import EncodedWorkflowResult, decode_workflow_result
from .run_worker import run_worker
from .source import is_watch_path_included, resolve_live_source, resolve_watch_roots
from .webhooks import DEFAULT_WEBHOOK_PORT, WebhookServer, routes_for
from .windows_job import WindowsJob, assign_process, close_job, create_kill_on_close_job

_LEVEL_MAP = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARN,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.ERROR,
}
logger = logging.getLogger(__name__)

ExecutorBackend: TypeAlias = Literal["local", "ray"]
STREAM_HISTORY_CAPACITY = 1024
DEFAULT_RESULT_RETENTION_SECONDS = 24 * 60 * 60
_PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.02
DETAIL_PAGE_SIZE = 100
MAX_DETAIL_PAGE_SIZE = 500
_DESCRIPTOR_PAGE_ORDER_FORWARD = 0
_DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST = 1
STRUCTURAL_BASELINE_CAPACITY = 8
SUBSCRIBER_QUEUE_CAPACITY = 256
MAX_RUN_ID_BYTES = 256
MAX_TRANSPORT_PAGE_BYTES = 2 * 1024 * 1024
MAX_AGENT_EVENT_BYTES = 8 * 1024 * 1024
MAX_TRACE_BODY_BYTES = 32 * 1024 * 1024
MAX_NODE_DETAIL_BYTES = 64 * 1024 * 1024
MAX_RUN_LOG_BYTES = 16 * 1024 * 1024
_RELOAD_LOG_DIAGNOSTIC_LIMIT = 5
_RELOAD_LOG_MESSAGE_LIMIT = 500
_RELOAD_LOG_SUMMARY_LIMIT = 2_000
MAX_RUN_LOG_ENTRIES = 100_000
MAX_RUN_DETAIL_BYTES = 128 * 1024 * 1024
MAX_AGENT_DETAIL_DEPTH = 64
_MISSING_WORKFLOW_ID = "\0"
DeprecatedExecutorFactory: TypeAlias = (
    type[LocalExecutor] | type[RayExecutor] | partial[RayExecutor]
)


class InvalidRunIdError(ValueError):
    """Raised when a caller-owned run ID cannot be retained safely."""


class RunAlreadyExistsError(ValueError):
    """Raised when a caller-owned run ID has already been reserved."""


class RunResultNotReadyError(RuntimeError):
    """Raised when result retrieval is attempted before terminal success."""


class RunResultUnavailableError(RuntimeError):
    """Raised when a failed or cancelled run has no workflow result."""


class StructuralBaselineUnavailableError(RuntimeError):
    """Raised when a client must restart structural snapshot synchronization."""


class _ExecutorBackendOmitted:
    pass


_EXECUTOR_BACKEND_OMITTED = _ExecutorBackendOmitted()


class _CoordinatorProtocolError(RuntimeError):
    """A coordinator event does not match the parent-side protocol."""


@dataclass
class _RunHandle:
    process: multiprocessing.Process
    event_queue: Any
    cancel_event: Any
    start_event: Any
    assignment_event: Any
    windows_job: WindowsJob | None
    result_bundle: PendingResultBundle
    publication_event: threading.Event
    drain_thread: threading.Thread | None = None
    preparation_thread: threading.Thread | None = None
    success_quiesced: bool = False


@dataclass(frozen=True)
class _ProcessGroupTeardown:
    quiesced: bool
    natural_exitcode: int | None = None

    def __bool__(self) -> bool:
        return self.quiesced


@dataclass(frozen=True)
class _RunNotifications:
    sequence: int
    run_callbacks: tuple[tuple[Callable[[RunState], None], RunState], ...]
    log_callbacks: tuple[tuple[Callable[[LogEntry], None], LogEntry], ...]
    detail_callbacks: tuple[tuple[Callable[[DetailUpdate], None], DetailUpdate], ...]
    update_subscribers: tuple[tuple[queue.Queue, tuple[OperatorUpdateEnvelope, ...]], ...]
    ready: threading.Event
    delivered: threading.Event


@dataclass(frozen=True)
class _AgentEvidenceMutation:
    entry: LogEntry
    agent_event: AgentEvent | None = None
    finalized_trace: bytes | None = None


@dataclass(frozen=True)
class _StructuralBaseline:
    as_of_sequence: int
    summaries: tuple[RunSummary, ...]
    snapshots: Mapping[str, RunSnapshot]
    summary_indexes: Mapping[str, tuple[RunSummary, ...]]


@dataclass(frozen=True)
class _RunDetailCapture:
    run: RunState
    logs: tuple[SequencedLogEntry, ...]
    events: Mapping[str, tuple[AgentEvent, ...]]
    trace_bodies: Mapping[str, bytes]
    trace_errors: Mapping[str, str | None]
    trace_invocation_ids: Mapping[str, str]


def _bound_reload_log_text(text: str) -> str:
    if len(text) <= _RELOAD_LOG_SUMMARY_LIMIT:
        return text
    return text[: _RELOAD_LOG_SUMMARY_LIMIT - 3] + "..."


def _summarize_reload_diagnostics(
    diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...],
) -> str:
    summaries = [
        f"{diagnostic.kind} {diagnostic.path}: "
        f"{diagnostic.message[:_RELOAD_LOG_MESSAGE_LIMIT]}"
        for diagnostic in diagnostics[:_RELOAD_LOG_DIAGNOSTIC_LIMIT]
    ]
    omitted = len(diagnostics) - len(summaries)
    if omitted:
        summaries.append(f"{omitted} additional diagnostic(s)")
    return _bound_reload_log_text("; ".join(summaries))


class Operator:
    """Discover workflows and coordinate each local run in a spawned process."""

    def __init__(
        self,
        workflow_paths: list[str] | None = None,
        executor_factory: DeprecatedExecutorFactory | None = None,
        watch: bool = True,
        schedule: bool = True,
        *,
        executor_backend: ExecutorBackend | _ExecutorBackendOmitted = (
            _EXECUTOR_BACKEND_OMITTED
        ),
        ray_runtime_env: Mapping[str, Any] | None = None,
        ray_init_kwargs: Mapping[str, Any] | None = None,
        prepare_timeout: float = 15.0,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        cancel_grace: float = 5.0,
        stream_history_capacity: int = STREAM_HISTORY_CAPACITY,
        result_storage_directory: str | os.PathLike[str] | None = None,
        result_retention_seconds: float | None = DEFAULT_RESULT_RETENTION_SECONDS,
        webhook_port: int = DEFAULT_WEBHOOK_PORT,
        structural_baseline_capacity: int = STRUCTURAL_BASELINE_CAPACITY,
        subscriber_queue_capacity: int = SUBSCRIBER_QUEUE_CAPACITY,
        max_agent_event_bytes: int = MAX_AGENT_EVENT_BYTES,
        max_trace_body_bytes: int = MAX_TRACE_BODY_BYTES,
        max_node_detail_bytes: int = MAX_NODE_DETAIL_BYTES,
        max_run_log_bytes: int = MAX_RUN_LOG_BYTES,
        max_run_log_entries: int = MAX_RUN_LOG_ENTRIES,
        max_run_detail_bytes: int = MAX_RUN_DETAIL_BYTES,
    ) -> None:
        """Create an operator with spawn-safe executor configuration.

        ``executor_factory`` is deprecated and only accepts the exact
        ``LocalExecutor`` or ``RayExecutor`` classes, or a keyword-only
        ``functools.partial(RayExecutor, ...)``. Use ``executor_backend`` and
        the Ray configuration mappings for new code.
        """
        self._executor_config = _resolve_executor_config(
            executor_backend=executor_backend,
            ray_runtime_env=ray_runtime_env,
            ray_init_kwargs=ray_init_kwargs,
            executor_factory=executor_factory,
        )
        if not 0 <= webhook_port <= 65535:
            raise ValueError("Webhook port must be between 0 and 65535")
        if cancel_grace < 0:
            raise ValueError("Cancellation grace must be non-negative")
        if stream_history_capacity <= 0:
            raise ValueError("Stream history capacity must be positive")
        if result_retention_seconds is not None:
            if isinstance(result_retention_seconds, bool) or not isinstance(
                result_retention_seconds, (int, float)
            ):
                raise TypeError("Result retention must be a real number or None")
            if not math.isfinite(result_retention_seconds) or result_retention_seconds <= 0:
                raise ValueError("Result retention must be positive and finite")
        if structural_baseline_capacity <= 0:
            raise ValueError("Structural baseline capacity must be positive")
        if subscriber_queue_capacity <= 0:
            raise ValueError("Subscriber queue capacity must be positive")
        if isinstance(max_run_log_entries, bool) or not isinstance(max_run_log_entries, int):
            raise TypeError("Run log entry limit must be an integer")
        if max_run_log_entries <= 0:
            raise ValueError("Run log entry limit must be positive")
        detail_limits = {
            "Agent event": max_agent_event_bytes,
            "Trace body": max_trace_body_bytes,
            "Node detail": max_node_detail_bytes,
            "Run log": max_run_log_bytes,
            "Run detail": max_run_detail_bytes,
        }
        for label, limit in detail_limits.items():
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError(f"{label} byte limit must be an integer")
            if limit <= 0:
                raise ValueError(f"{label} byte limit must be positive")
        if max_node_detail_bytes < max(max_agent_event_bytes, max_trace_body_bytes):
            raise ValueError("Node detail byte limit must contain the largest detail body")
        if max_run_detail_bytes < max(max_node_detail_bytes, max_run_log_bytes):
            raise ValueError("Run detail byte limit must contain node detail and logs")
        self._max_agent_event_bytes = max_agent_event_bytes
        self._max_trace_body_bytes = max_trace_body_bytes
        self._max_node_detail_bytes = max_node_detail_bytes
        self._max_run_log_bytes = max_run_log_bytes
        self._max_run_log_entries = max_run_log_entries
        self._max_run_detail_bytes = max_run_detail_bytes
        self._cancel_grace = cancel_grace
        self._result_retention_seconds = (
            None if result_retention_seconds is None else float(result_retention_seconds)
        )
        self._prepare_timeout = prepare_timeout
        self._registry = WorkflowRegistry(discovery_timeout=discovery_timeout)
        self._workflow_paths = workflow_paths or []
        self._webhooks = WebhookServer(self, webhook_port)
        self._webhook_routes = {}
        initial_view = None
        if self._workflow_paths:
            initial_view = self._registry.scan(self._workflow_paths, validate=routes_for)
        self._subscriber_queue_capacity = subscriber_queue_capacity
        self._mp = multiprocessing.get_context("spawn")
        self._runs: dict[str, RunState] = {}
        self._stored_results: dict[str, StoredWorkflowResult] = {}
        self._result_leases: dict[str, int] = {}
        self._run_created_sequences: dict[str, int] = {}
        self._run_revisions: dict[str, int] = {}
        self._run_activity_sequences: dict[str, int] = {}
        self._node_revisions: dict[tuple[str, str], int] = {}
        self._logs: dict[str, list[SequencedLogEntry]] = {}
        self._log_sequences_by_node: dict[str, dict[str, list[int]]] = {}
        self._agent_events: dict[tuple[str, str], list[AgentEvent]] = {}
        self._trace_descriptors: dict[tuple[str, str], TraceDescriptor] = {}
        self._trace_bodies: dict[tuple[str, str], dict[int, bytes]] = {}
        self._trace_errors: dict[tuple[str, str], str | None] = {}
        self._agent_invocation_sequences: dict[tuple[str, str, str], int] = {}
        self._trace_invocation_ids: dict[tuple[str, str], str] = {}
        self._run_log_bytes: dict[str, int] = {}
        self._run_detail_bytes: dict[str, int] = {}
        self._node_detail_bytes: dict[tuple[str, str], int] = {}
        self._active_runs: dict[str, _RunHandle] = {}
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._detail_callbacks: list[Callable[[DetailUpdate], None]] = []
        self._update_subscribers: list[queue.Queue] = []
        self._operator_instance_id = uuid4().hex
        self._sequence = 0
        self._catalog_sequence = 0
        self._stream_history: deque[OperatorUpdate] = deque(maxlen=stream_history_capacity)
        self._structural_baseline_capacity = structural_baseline_capacity
        self._structural_baselines: OrderedDict[int, _StructuralBaseline] = OrderedDict()
        self._lock = threading.RLock()
        self._watcher_stop = threading.Event()
        self._watcher_ready = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._result_cleanup_stop = threading.Event()
        self._result_cleanup_thread: threading.Thread | None = None
        self._closed = False
        self._result_store = ResultStore(result_storage_directory)
        self._notification_stop_enqueued = False
        self._notification_shutdown_thread: threading.Thread | None = None
        self._notification_queue: queue.Queue[_RunNotifications | None] = queue.Queue()
        self._notification_thread: threading.Thread | None = None

        from .scheduler import Scheduler

        self._scheduler = Scheduler(self)
        try:
            self._scheduler.reconcile(self._registry.descriptors())
            if initial_view is not None:
                self._reconcile_webhooks(initial_view.by_id.values())
            if watch and self._workflow_paths:
                self._start_watcher()
            self._start_notification_dispatcher()
            if schedule and self._workflow_paths:
                self._scheduler.start()
            if self._result_retention_seconds is not None:
                self._result_cleanup_thread = threading.Thread(
                    target=self._result_cleanup_loop,
                    name="avalanche-result-cleanup",
                    daemon=True,
                )
                self._result_cleanup_thread.start()
        except BaseException:
            self.close()
            raise

    @property
    def operator_instance_id(self) -> str:
        return self._operator_instance_id

    @property
    def current_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def update_history_bounds(self) -> tuple[int, int]:
        """Return the retained lifecycle update interval for V2 cursor validation."""
        with self._lock:
            return self._history_bounds_locked()

    def retained_structural_floor(self) -> int:
        """Return the oldest structural baseline that can still be continued."""
        with self._lock:
            self._materialize_structural_baseline_locked()
            return next(iter(self._structural_baselines), self._sequence)

    def has_retained_structural_baseline(self, as_of_sequence: int) -> bool:
        """Whether one V2 structural continuation still has its exact baseline."""
        with self._lock:
            return as_of_sequence in self._structural_baselines

    def get_catalog(self) -> CatalogSnapshot:
        """Return one complete, revisioned current-workflow catalog."""
        with self._lock:
            return self._catalog_snapshot(self._registry.view, self._catalog_sequence)

    def _catalog_snapshot(self, view: CatalogView, as_of_sequence: int) -> CatalogSnapshot:
        # get_catalog() and _publish_catalog() call this while self._lock is held.
        # Own registry projections before adding dynamic schedule/webhook state.
        workflows = deepcopy(self._registry.list_workflows(view))
        for info in workflows:
            route = next(
                (
                    item
                    for item in self._webhook_routes.values()
                    if item.workflow_id == info.workflow_id
                ),
                None,
            )
            if route is not None:
                info.webhook_path = route.path
                info.webhook_url = self._webhooks.url_for(route.path)
                info.webhook_active = self._webhooks.active
            if info.cron:
                nxt = self._scheduler.next_run_time(info.cron)
                info.next_run_at = nxt.timestamp() if nxt else None
                info.last_run_at = self._scheduler.last_triggered(info.workflow_id)
        return CatalogSnapshot(
            revision=view.revision,
            operator_instance_id=self._operator_instance_id,
            as_of_sequence=as_of_sequence,
            workflows=tuple(workflows),
            scan_targets=view.scan_targets,
            diagnostics=view.diagnostics,
        )

    def list_workflows(self) -> list[WorkflowInfo]:
        return list(self.get_catalog().workflows)

    def list_diagnostics(self):
        return list(self.get_catalog().diagnostics)

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        with self._lock:
            captures = [self._capture_run_detail_locked(run) for run in self._runs.values()]
        runs = [_materialize_run_detail(capture) for capture in captures]
        return self._matching_runs(runs, workflow_selector)

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            run = self._runs.get(run_id)
            capture = self._capture_run_detail_locked(run) if run is not None else None
        return _materialize_run_detail(capture) if capture is not None else None

    def list_run_summaries(
        self,
        workflow_selector: str = "",
        *,
        page_size: int = 0,
        page_token: str = "",
    ) -> RunSummaryPage:
        """Return one page from an immutable retained structural baseline."""
        size = _bounded_page_size(page_size)
        with self._lock:
            if page_token:
                token = _decode_page_token(page_token)
                if token["operator_instance_id"] != self._operator_instance_id:
                    raise StructuralBaselineUnavailableError(
                        "Operator instance changed; restart snapshot synchronization"
                    )
                if token["workflow_selector"] != workflow_selector:
                    raise ValueError("Page token workflow selector does not match request")
                baseline = self._retained_structural_baseline_locked(token["as_of_sequence"])
                resolved_workflow_id = token["resolved_workflow_id"]
                cursor_sequence = token["created_sequence"]
                cursor_run_id = token["run_id"]
            else:
                baseline = self._materialize_structural_baseline_locked()
                resolved_workflow_id = self._resolve_summary_workflow_id(
                    baseline.summaries, workflow_selector
                )
                cursor_sequence = None
                cursor_run_id = ""

            summaries = baseline.summary_indexes.get(resolved_workflow_id, ())
            start = 0
            if cursor_sequence is not None:
                start = bisect_right(
                    summaries,
                    (-cursor_sequence, cursor_run_id),
                    key=lambda item: (-item.created_sequence, item.run_id),
                )
            candidates = summaries[start : start + size + 1]
            selected = _take_bounded_summaries(candidates, size)
            next_page_token = ""
            if selected and start + len(selected) < len(summaries):
                last = selected[-1]
                next_page_token = _encode_page_token(
                    operator_instance_id=self._operator_instance_id,
                    as_of_sequence=baseline.as_of_sequence,
                    workflow_selector=workflow_selector,
                    resolved_workflow_id=resolved_workflow_id,
                    created_sequence=last.created_sequence,
                    run_id=last.run_id,
                )
            return RunSummaryPage(
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=baseline.as_of_sequence,
                runs=tuple(selected),
                next_page_token=next_page_token,
            )

    def get_run_snapshot(
        self,
        run_id: str,
        *,
        operator_instance_id: str,
        as_of_sequence: int,
    ) -> RunSnapshot | None:
        """Return one run from an exact retained structural baseline."""
        with self._lock:
            if operator_instance_id != self._operator_instance_id:
                raise StructuralBaselineUnavailableError(
                    "Operator instance changed; restart snapshot synchronization"
                )
            baseline = self._retained_structural_baseline_locked(as_of_sequence)
            return baseline.snapshots.get(run_id)

    def get_latest_run_snapshot(
        self,
        run_id: str,
        *,
        operator_instance_id: str,
    ) -> RunSnapshot | None:
        """Return the latest structural run and detail watermarks atomically."""
        with self._lock:
            if operator_instance_id != self._operator_instance_id:
                raise StructuralBaselineUnavailableError(
                    "Operator instance changed; restart snapshot synchronization"
                )
            run = self._runs.get(run_id)
            if run is None:
                return None
            as_of_sequence = self._sequence
            return self._run_snapshot_locked(
                run,
                summary=self._run_summary_locked(run),
                as_of_sequence=as_of_sequence,
            )

    def list_logs(
        self,
        run_id: str = "",
        *,
        page_token: str = "",
        after_sequence: int = 0,
        page_size: int = 0,
        before_sequence: int = 0,
        node_id: str = "",
        order: int = _DESCRIPTOR_PAGE_ORDER_FORWARD,
    ) -> LogPage:
        """Return a byte-bounded page of immutable log body descriptors."""
        size = _bounded_page_size(page_size)
        order = _validated_descriptor_page_order(order)
        after_sequence = _validated_descriptor_cursor(after_sequence, "after_sequence")
        before_sequence = _validated_descriptor_cursor(before_sequence, "before_sequence")
        if order == _DESCRIPTOR_PAGE_ORDER_FORWARD and before_sequence:
            raise ValueError("before_sequence is only valid for newest-first pages")
        if order == _DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST and after_sequence:
            raise ValueError("after_sequence is only valid for forward pages")
        with self._lock:
            token = (
                _decode_transport_token(page_token, "logs")
                if page_token
                else self._current_log_page_token_locked(run_id)
            )
            self._validate_transport_token_locked(token)
            run_id = token["run_id"]
            through_sequence = token["through_sequence"]
            if "cursor" in token:
                if token["order"] != order:
                    raise ValueError("Page order does not match the continuation token")
                if token["node_id"] != node_id:
                    raise ValueError("Log node filter does not match the continuation token")
                requested_cursor = (
                    after_sequence
                    if order == _DESCRIPTOR_PAGE_ORDER_FORWARD
                    else before_sequence
                )
                if requested_cursor and requested_cursor != token["cursor"]:
                    raise ValueError("Page cursor does not match the continuation token")
                cursor = token["cursor"]
            else:
                cursor = (
                    after_sequence
                    if order == _DESCRIPTOR_PAGE_ORDER_FORWARD
                    else before_sequence or through_sequence + 1
                )
            logs = self._logs.get(run_id)
            if logs is None and run_id not in self._runs:
                raise KeyError(run_id)
            logs = logs or []
            candidates: list[SequencedLogEntry]
            if node_id:
                run_node_sequences = self._log_sequences_by_node.get(run_id)
                node_sequences = (
                    () if run_node_sequences is None else run_node_sequences.get(node_id, ())
                )
                if order == _DESCRIPTOR_PAGE_ORDER_FORWARD:
                    start = bisect_right(node_sequences, cursor)
                    stop = bisect_right(node_sequences, through_sequence)
                    selected_sequences = node_sequences[start : min(start + size + 1, stop)]
                else:
                    stop = bisect_right(
                        node_sequences,
                        min(cursor - 1, through_sequence),
                    )
                    start = max(0, stop - size - 1)
                    selected_sequences = reversed(node_sequences[start:stop])
                candidates = [logs[sequence - 1] for sequence in selected_sequences]
            elif order == _DESCRIPTOR_PAGE_ORDER_FORWARD:
                start = min(cursor, len(logs))
                stop = min(start + size + 1, through_sequence, len(logs))
                candidates = logs[start:stop]
            else:
                start = min(cursor - 1, through_sequence, len(logs)) - 1
                stop = max(-1, start - size - 1)
                candidates = [logs[index] for index in range(start, stop, -1)]
            descriptors = [
                self._log_descriptor_locked(
                    run_id,
                    item,
                    as_of_sequence=token["as_of_sequence"],
                )
                for item in candidates
            ]
            selected = _take_bounded_descriptors(descriptors, size)
            next_page_token = ""
            if selected and len(selected) < len(descriptors):
                next_page_token = _encode_transport_token(
                    **{
                        **token,
                        "node_id": node_id,
                        "order": order,
                        "cursor": selected[-1].sequence,
                    }
                )
            return LogPage(
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=token["as_of_sequence"],
                logs=tuple(selected),
                next_page_token=next_page_token,
            )

    def list_agent_events(
        self,
        run_id: str = "",
        node_id: str = "",
        *,
        page_token: str = "",
        after_event_sequence: int = 0,
        page_size: int = 0,
        before_event_sequence: int = 0,
        order: int = _DESCRIPTOR_PAGE_ORDER_FORWARD,
    ) -> AgentEventPage:
        """Return a byte-bounded page of immutable event body descriptors."""
        size = _bounded_page_size(page_size)
        order = _validated_descriptor_page_order(order)
        after_event_sequence = _validated_descriptor_cursor(
            after_event_sequence, "after_event_sequence"
        )
        before_event_sequence = _validated_descriptor_cursor(
            before_event_sequence, "before_event_sequence"
        )
        if order == _DESCRIPTOR_PAGE_ORDER_FORWARD and before_event_sequence:
            raise ValueError("before_event_sequence is only valid for newest-first pages")
        if order == _DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST and after_event_sequence:
            raise ValueError("after_event_sequence is only valid for forward pages")
        with self._lock:
            token = (
                _decode_transport_token(page_token, "events")
                if page_token
                else self._current_event_page_token_locked(run_id, node_id)
            )
            self._validate_transport_token_locked(token)
            run_id = token["run_id"]
            node_id = token["node_id"]
            through_sequence = token["through_sequence"]
            if "cursor" in token:
                if token["order"] != order:
                    raise ValueError("Page order does not match the continuation token")
                requested_cursor = (
                    after_event_sequence
                    if order == _DESCRIPTOR_PAGE_ORDER_FORWARD
                    else before_event_sequence
                )
                if requested_cursor and requested_cursor != token["cursor"]:
                    raise ValueError("Page cursor does not match the continuation token")
                cursor = token["cursor"]
            else:
                cursor = (
                    after_event_sequence
                    if order == _DESCRIPTOR_PAGE_ORDER_FORWARD
                    else before_event_sequence or through_sequence + 1
                )
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if node_id not in run.nodes:
                raise KeyError(node_id)
            events = self._agent_events.get((run_id, node_id), [])
            if order == _DESCRIPTOR_PAGE_ORDER_FORWARD:
                start = bisect_right(
                    events,
                    cursor,
                    key=lambda item: item.event_sequence,
                )
                stop = min(start + size + 1, len(events))
                candidates = [
                    item
                    for item in events[start:stop]
                    if item.event_sequence <= through_sequence
                ]
            else:
                upper = bisect_right(
                    events,
                    min(cursor - 1, through_sequence),
                    key=lambda item: item.event_sequence,
                )
                candidates = list(reversed(events[max(0, upper - size - 1) : upper]))
            descriptors = [
                self._agent_event_descriptor_locked(
                    run_id,
                    node_id,
                    item,
                    as_of_sequence=token["as_of_sequence"],
                )
                for item in candidates
            ]
            selected = _take_bounded_descriptors(descriptors, size)
            next_page_token = ""
            if selected and len(selected) < len(descriptors):
                next_page_token = _encode_transport_token(
                    **{
                        **token,
                        "order": order,
                        "cursor": selected[-1].event_sequence,
                    }
                )
            return AgentEventPage(
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=token["as_of_sequence"],
                run_id=run_id,
                node_id=node_id,
                events=tuple(selected),
                next_page_token=next_page_token,
            )

    def read_detail(self, body_token: str) -> bytes:
        """Resolve one immutable log or event body from an opaque token."""
        token = _decode_transport_token(body_token, {"log-body", "event-body"})
        with self._lock:
            self._validate_transport_token_locked(token)
            run_id = token["run_id"]
            sequence = token["sequence"]
            if token["kind"] == "log-body":
                logs = self._logs.get(run_id, ())
                if sequence < 1 or sequence > len(logs):
                    raise KeyError(sequence)
                body = logs[sequence - 1].entry.message
            else:
                node_id = token["node_id"]
                events = self._agent_events.get((run_id, node_id), ())
                index = bisect_right(
                    events,
                    sequence - 1,
                    key=lambda item: item.event_sequence,
                )
                if index >= len(events) or events[index].event_sequence != sequence:
                    raise KeyError(sequence)
                body = events[index].event_json
        return body.encode()

    def read_trace(
        self,
        run_id: str,
        node_id: str,
        *,
        operator_instance_id: str,
        revision: int = 0,
    ) -> FinalizedTrace:
        """Copy one immutable finalized trace body out of operator-owned memory."""
        with self._lock:
            if operator_instance_id != self._operator_instance_id:
                raise StructuralBaselineUnavailableError(
                    "Operator instance changed; restart snapshot synchronization"
                )
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if node_id not in run.nodes:
                raise KeyError(node_id)
            versions = self._trace_bodies.get((run_id, node_id), {})
            selected_revision = revision or (max(versions) if versions else 0)
            try:
                data = versions[selected_revision]
            except KeyError:
                raise KeyError(selected_revision) from None
            return FinalizedTrace(revision=selected_revision, data=data)

    def _materialize_structural_baseline_locked(self) -> _StructuralBaseline:
        as_of_sequence = self._sequence
        retained = self._structural_baselines.get(as_of_sequence)
        if retained is not None:
            self._structural_baselines.move_to_end(as_of_sequence)
            return retained

        summaries = []
        snapshots = {}
        summary_indexes: dict[str, list[RunSummary]] = {"": []}
        for run in self._runs.values():
            summary = self._run_summary_locked(run)
            summaries.append(summary)
            summary_indexes[""].append(summary)
            workflow_id = summary.workflow_id or summary.flow_name
            summary_indexes.setdefault(workflow_id, []).append(summary)
            snapshots[run.run_id] = self._run_snapshot_locked(
                run,
                summary=summary,
                as_of_sequence=as_of_sequence,
            )
        frozen_indexes = {
            workflow_id: tuple(
                sorted(items, key=lambda item: (-item.created_sequence, item.run_id))
            )
            for workflow_id, items in summary_indexes.items()
        }
        baseline = _StructuralBaseline(
            as_of_sequence=as_of_sequence,
            summaries=frozen_indexes[""],
            snapshots=MappingProxyType(snapshots),
            summary_indexes=MappingProxyType(frozen_indexes),
        )
        self._structural_baselines[as_of_sequence] = baseline
        while len(self._structural_baselines) > self._structural_baseline_capacity:
            self._structural_baselines.popitem(last=False)
        return baseline

    def _retained_structural_baseline_locked(self, as_of_sequence: int) -> _StructuralBaseline:
        try:
            baseline = self._structural_baselines[as_of_sequence]
        except KeyError:
            raise StructuralBaselineUnavailableError(
                f"Structural baseline {as_of_sequence} is unavailable; "
                "restart snapshot synchronization"
            ) from None
        self._structural_baselines.move_to_end(as_of_sequence)
        return baseline

    def _run_snapshot_locked(
        self,
        run: RunState,
        *,
        summary: RunSummary,
        as_of_sequence: int,
    ) -> RunSnapshot:
        nodes = tuple(
            NodeSnapshot(
                node_id=node.node_id,
                name=node.name,
                node_type=node.node_type,
                status=node.status,
                started_at=node.started_at,
                ended_at=node.ended_at,
                running_elapsed_seconds=(
                    node.elapsed if node.status is NodeStatus.RUNNING else None
                ),
                error=node.error,
                trace=self._trace_descriptors.get((run.run_id, node.node_id)),
                revision=self._node_revisions.get(
                    (run.run_id, node.node_id),
                    self._run_created_sequences.get(run.run_id, 0),
                ),
                event_page_token=self._event_page_token_locked(
                    run.run_id,
                    node.node_id,
                    as_of_sequence,
                ),
            )
            for node in run.nodes.values()
        )
        logs = self._logs.get(run.run_id, ())
        latest_log_sequence = logs[-1].sequence if logs else 0
        return RunSnapshot(
            operator_instance_id=self._operator_instance_id,
            as_of_sequence=as_of_sequence,
            summary=summary,
            nodes=nodes,
            latest_log_sequence=latest_log_sequence,
            log_page_token=_encode_transport_token(
                kind="logs",
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=as_of_sequence,
                run_id=run.run_id,
                through_sequence=latest_log_sequence,
            ),
            topology=run.topology,
            terminal_seal=run.terminal_seal,
        )

    def _current_log_page_token_locked(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._runs:
            raise KeyError(run_id)
        logs = self._logs.get(run_id, ())
        return {
            "v": 1,
            "kind": "logs",
            "operator_instance_id": self._operator_instance_id,
            "as_of_sequence": self._sequence,
            "run_id": run_id,
            "through_sequence": logs[-1].sequence if logs else 0,
        }

    def _current_event_page_token_locked(self, run_id: str, node_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if node_id not in run.nodes:
            raise KeyError(node_id)
        return _decode_transport_token(
            self._event_page_token_locked(run_id, node_id, self._sequence),
            "events",
        )

    def _event_page_token_locked(
        self,
        run_id: str,
        node_id: str,
        as_of_sequence: int,
    ) -> str:
        events = self._agent_events.get((run_id, node_id), ())
        return _encode_transport_token(
            kind="events",
            operator_instance_id=self._operator_instance_id,
            as_of_sequence=as_of_sequence,
            run_id=run_id,
            node_id=node_id,
            through_sequence=events[-1].event_sequence if events else 0,
        )

    def _validate_transport_token_locked(self, token: Mapping[str, Any]) -> None:
        if token["operator_instance_id"] != self._operator_instance_id:
            raise StructuralBaselineUnavailableError(
                "Operator instance changed; restart detail hydration"
            )
        run_id = token["run_id"]
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        node_id = token.get("node_id")
        if node_id and node_id not in run.nodes:
            raise KeyError(node_id)

    def _log_descriptor_locked(
        self,
        run_id: str,
        item: SequencedLogEntry,
        *,
        as_of_sequence: int,
    ) -> LogRecordDescriptor:
        return LogRecordDescriptor(
            sequence=item.sequence,
            timestamp=item.entry.timestamp,
            level=item.entry.level,
            node_id=item.entry.node_id,
            size_bytes=item.size_bytes,
            body_token=_encode_transport_token(
                kind="log-body",
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=as_of_sequence,
                run_id=run_id,
                sequence=item.sequence,
            ),
        )

    def _agent_event_descriptor_locked(
        self,
        run_id: str,
        node_id: str,
        item: AgentEvent,
        *,
        as_of_sequence: int,
    ) -> AgentEventDescriptor:
        return AgentEventDescriptor(
            invocation_id=item.invocation_id,
            event_sequence=item.event_sequence,
            size_bytes=item.size_bytes,
            body_token=_encode_transport_token(
                kind="event-body",
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=as_of_sequence,
                run_id=run_id,
                node_id=node_id,
                sequence=item.event_sequence,
            ),
            event_kind=item.event_kind,
            iteration=item.iteration,
            duration_ms=item.duration_ms,
            error=item.error,
            tool_count=item.tool_count,
            predict_count=item.predict_count,
        )

    def _capture_run_detail_locked(self, run: RunState) -> _RunDetailCapture:
        captured_run = deepcopy(run)
        trace_bodies = {}
        trace_errors = {}
        trace_invocation_ids = {}
        events = {}
        for node_id in run.nodes:
            key = (run.run_id, node_id)
            events[node_id] = tuple(self._agent_events.get(key, ()))
            descriptor = self._trace_descriptors.get(key)
            captured_run.nodes[node_id].trace = descriptor
            if descriptor is not None and descriptor.available:
                body = self._trace_bodies.get(key, {}).get(descriptor.revision)
                if body is not None:
                    trace_bodies[node_id] = body
            trace_errors[node_id] = self._trace_errors.get(key)
            trace_invocation_ids[node_id] = self._trace_invocation_ids.get(key, "")
        return _RunDetailCapture(
            run=captured_run,
            logs=tuple(self._logs.get(run.run_id, ())),
            events=MappingProxyType(events),
            trace_bodies=MappingProxyType(trace_bodies),
            trace_errors=MappingProxyType(trace_errors),
            trace_invocation_ids=MappingProxyType(trace_invocation_ids),
        )

    def _resolve_summary_workflow_id(
        self,
        summaries: tuple[RunSummary, ...],
        workflow_selector: str,
    ) -> str:
        if not workflow_selector:
            return ""
        if any(summary.workflow_id == workflow_selector for summary in summaries):
            return workflow_selector
        try:
            return self._registry.resolve(workflow_selector).workflow_id
        except AmbiguousWorkflow:
            raise
        except KeyError:
            matching_ids = sorted(
                {
                    summary.workflow_id or summary.flow_name
                    for summary in summaries
                    if summary.flow_name == workflow_selector
                    or summary.workflow_display_name == workflow_selector
                }
            )
            if len(matching_ids) > 1:
                raise AmbiguousWorkflow(workflow_selector, tuple(matching_ids)) from None
            return matching_ids[0] if matching_ids else _MISSING_WORKFLOW_ID

    def _matching_runs(self, runs: list[RunState], workflow_selector: str) -> list[RunState]:
        if not workflow_selector:
            return runs
        exact = [run for run in runs if run.workflow_id == workflow_selector]
        if exact:
            return exact
        try:
            workflow_id = self._registry.resolve(workflow_selector).workflow_id
        except AmbiguousWorkflow:
            raise
        except KeyError:
            matching_ids = sorted(
                {
                    run.workflow_id or run.flow_name
                    for run in runs
                    if run.flow_name == workflow_selector
                    or run.workflow_display_name == workflow_selector
                }
            )
            if len(matching_ids) > 1:
                raise AmbiguousWorkflow(workflow_selector, tuple(matching_ids)) from None
            if not matching_ids:
                return []
            workflow_id = matching_ids[0]
        return [run for run in runs if (run.workflow_id or run.flow_name) == workflow_id]

    def _run_summary_locked(self, run: RunState) -> RunSummary:
        return RunSummary(
            run_id=run.run_id,
            flow_name=run.flow_name,
            status=run.status,
            triggered_at=run.triggered_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            triggered_by=run.triggered_by,
            workflow_id=run.workflow_id,
            workflow_display_name=run.workflow_display_name,
            created_sequence=self._run_created_sequences.get(run.run_id, 0),
            revision=self._run_revisions.get(run.run_id, 0),
        )

    def get_run_result(self, run_id: str) -> Any:
        """Return a successful run's decoded workflow result.

        Results are available only after terminal success. Failed, cancelled,
        pending, and unknown runs retain their existing state semantics and do
        not expose a partial payload.
        """
        return decode_workflow_result(self._get_run_result_payload(run_id))

    def _get_run_result_payload(self, run_id: str) -> EncodedWorkflowResult:
        """Load one validated result payload from private storage."""
        with self._lock:
            run = self._runs.get(run_id)
            stored = self._stored_results.get(run_id)
            if run is None:
                raise KeyError(run_id)
            status = run.status
            if status in {RunStatus.REQUESTING, RunStatus.PENDING, RunStatus.RUNNING}:
                raise RunResultNotReadyError(f"Run {run_id} is not terminal")
            if status != RunStatus.SUCCESS:
                raise RunResultUnavailableError(
                    f"Run {run_id} ended with status {status.value}"
                )
            if stored is None:
                raise RunResultUnavailableError(
                    f"Run {run_id} result is unavailable or expired"
                )
            self._result_leases[run_id] = self._result_leases.get(run_id, 0) + 1
        try:
            return self._result_store.load(
                stored,
                cancel_signal=self._result_cleanup_stop,
            )
        finally:
            with self._lock:
                remaining = self._result_leases.get(run_id, 1) - 1
                if remaining:
                    self._result_leases[run_id] = remaining
                else:
                    self._result_leases.pop(run_id, None)

    def start_run(
        self,
        flow_name: str,
        triggered_by: str = "manual",
        *,
        run_id: str | None = None,
        input: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Publish a requesting run, then prepare it asynchronously."""
        if run_id is not None and not isinstance(run_id, str):
            raise InvalidRunIdError("run_id must be a string")
        if run_id and len(run_id.encode("utf-8")) > MAX_RUN_ID_BYTES:
            raise InvalidRunIdError(f"run_id exceeds {MAX_RUN_ID_BYTES}-byte UTF-8 limit")
        triggered_at = time.time()
        descriptor, configured_root = self._registry.resolve_source(flow_name)
        import_root, workflow_relative_module_file = resolve_live_source(
            configured_root, descriptor.locator
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Operator is closed")
            run_id = run_id or f"run_{str(uuid4())[:8]}"
            if run_id in self._runs or run_id in self._active_runs:
                raise RunAlreadyExistsError(f"Run {run_id} already exists")
            require_worker_descriptor_transfer()
            event_queue = self._mp.Queue()
            cancel_event = self._mp.Event()
            start_event = self._mp.Event()
            assignment_event = self._mp.Event()
            result_bundle = self._result_store.prepare()
            try:
                transferred_result_bundle = duplicate_bundle_descriptor_for_spawn(result_bundle)
                process = self._mp.Process(
                    target=run_worker,
                    args=(
                        str(import_root),
                        workflow_relative_module_file,
                        descriptor.locator.builder_symbol,
                        run_id,
                        self._executor_config,
                        assignment_event,
                        input,
                        context,
                        event_queue,
                        cancel_event,
                        start_event,
                        transferred_result_bundle,
                        (result_bundle.device, result_bundle.inode),
                    ),
                    name=f"avalanche-run-{run_id}",
                    daemon=False,
                )
                windows_job = create_kill_on_close_job()
            except BaseException:
                self._result_store.discard(result_bundle)
                _close_event_queue(event_queue)
                raise
            handle = _RunHandle(
                process=process,
                event_queue=event_queue,
                cancel_event=cancel_event,
                start_event=start_event,
                assignment_event=assignment_event,
                windows_job=windows_job,
                result_bundle=result_bundle,
                publication_event=threading.Event(),
            )
            # Reserve the ID before releasing the lock so concurrent callers
            # cannot create a second coordinator with the same caller-owned ID.
            self._active_runs[run_id] = handle
            self._log_sequences_by_node.pop(run_id, None)
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while creating run")
                process.start()
                if process.pid is None:
                    raise RuntimeError("Run coordinator did not expose a process ID")
                assign_process(windows_job, process.pid)
                assignment_event.set()
                run = RunState(
                    run_id=run_id,
                    flow_name=descriptor.display_name,
                    status=RunStatus.REQUESTING,
                    triggered_at=triggered_at,
                    triggered_by=triggered_by,
                    workflow_id=descriptor.workflow_id,
                    workflow_display_name=descriptor.display_name,
                )
                self._runs[run_id] = run
                notifications = self._publish_run_locked(run)
            self._wait_for_notifications(notifications)
            preparation_thread = threading.Thread(
                target=self._prepare_requested_run,
                args=(
                    run_id,
                    descriptor.workflow_id,
                    descriptor.display_name,
                    triggered_by,
                    triggered_at,
                    handle,
                ),
                name=f"avalanche-prepare-{run_id}",
                daemon=True,
            )
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while creating run")
                handle.preparation_thread = preparation_thread
                preparation_thread.start()
            return run_id
        except BaseException:
            cancel_event.set()
            handle.publication_event.set()
            handle.start_event.set()
            _teardown_process_group(process, windows_job)
            with self._lock:
                self._runs.pop(run_id, None)
                self._log_sequences_by_node.pop(run_id, None)
                self._stored_results.pop(run_id, None)
                self._active_runs.pop(run_id, None)
            self._result_store.discard(result_bundle)
            _close_event_queue(event_queue)
            raise

    def _prepare_requested_run(
        self,
        run_id: str,
        workflow_id: str,
        catalog_display_name: str,
        triggered_by: str,
        triggered_at: float,
        handle: _RunHandle,
    ) -> None:
        try:
            prepared, buffered_events = self._await_prepared(handle)
            prepared_run = self._run_from_prepared(
                run_id,
                workflow_id,
                catalog_display_name,
                triggered_by,
                triggered_at,
                prepared,
            )
            drain = threading.Thread(
                target=self._drain_run_events,
                args=(run_id, handle, buffered_events),
                name=f"avalanche-drain-{run_id}",
                daemon=True,
            )
            with self._lock:
                run = self._runs.get(run_id)
                if run is None:
                    return
                if self._closed:
                    raise RuntimeError("Operator closed while preparing run")
                run.flow_name = prepared_run.flow_name
                run.workflow_display_name = prepared_run.workflow_display_name
                run.topology = prepared_run.topology
                run.nodes = prepared_run.nodes
                if handle.cancel_event.is_set():
                    run.status = RunStatus.CANCELLED
                    run.ended_at = time.monotonic()
                else:
                    run.status = RunStatus.PENDING
                handle.drain_thread = drain
                notifications = self._publish_run_locked(run)
            self._wait_for_notifications(notifications)
            drain.start()
            handle.publication_event.set()
            handle.start_event.set()
        except BaseException as exc:
            self._finish_requested_preparation_failure(run_id, handle, exc)

    def _finish_requested_preparation_failure(
        self,
        run_id: str,
        handle: _RunHandle,
        exc: BaseException,
    ) -> None:
        cancelled = handle.cancel_event.is_set()
        entry = LogEntry(
            timestamp=datetime.now(),
            level=LogLevel.ERROR,
            node_id="operator",
            message=f"Workflow preparation failed: {exc}",
        )
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in {
                RunStatus.SUCCESS,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                notifications = None
            else:
                run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
                run.ended_at = time.monotonic()
                log_entry = None
                if not cancelled:
                    self._append_log_locked(run, entry)
                    log_entry = entry
                notifications = self._publish_run_locked(run, log_entry=log_entry)
            self._active_runs.pop(run_id, None)
        handle.cancel_event.set()
        handle.publication_event.set()
        handle.start_event.set()
        _teardown_process_group(handle.process, handle.windows_job)
        self._result_store.discard(handle.result_bundle)
        _close_event_queue(handle.event_queue)
        if notifications is not None:
            self._wait_for_notifications(notifications)

    def cancel_run(self, run_id: str) -> None:
        with self._lock:
            handle = self._active_runs.get(run_id)
            run = self._runs.get(run_id)
            if handle is not None:
                already_requested = handle.cancel_event.is_set()
                handle.cancel_event.set()
                handle.start_event.set()
            else:
                already_requested = True
            if run is None or run.status not in {
                RunStatus.REQUESTING,
                RunStatus.PENDING,
                RunStatus.RUNNING,
            }:
                return
        if handle is not None and not already_requested:
            threading.Thread(
                target=self._force_cancel_after_grace,
                args=(run_id, handle),
                name=f"avalanche-cancel-{run_id}",
                daemon=True,
            ).start()

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        with self._lock:
            self._run_callbacks.append(callback)

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        with self._lock:
            self._log_callbacks.append(callback)

    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None:
        with self._lock:
            self._detail_callbacks.append(callback)

    def start_stream(self) -> None:
        """In-process callbacks are already live once registered."""

    def subscribe_operator_updates(
        self, operator_instance_id: str = "", after_sequence: int = 0
    ) -> queue.Queue:
        """Atomically replay retained updates or require a structural reset."""
        subscription: queue.Queue = queue.Queue(maxsize=self._subscriber_queue_capacity)
        with self._lock:
            history_floor, latest_sequence = self._history_bounds_locked()
            epoch_is_stale = operator_instance_id != self._operator_instance_id and (
                bool(operator_instance_id) or after_sequence != 0
            )
            cursor_is_stale = (
                after_sequence > latest_sequence or after_sequence < history_floor - 1
            )
            replay = [
                update for update in self._stream_history if update.sequence > after_sequence
            ]
            if (
                epoch_is_stale
                or cursor_is_stale
                or len(replay) > self._subscriber_queue_capacity
            ):
                subscription.put_nowait(self._update_reset_locked())
                return subscription

            for update in replay:
                subscription.put_nowait(
                    OperatorUpdateEnvelope(
                        operator_instance_id=self._operator_instance_id,
                        update=update,
                    )
                )
            self._update_subscribers.append(subscription)
        return subscription

    def _history_bounds_locked(self) -> tuple[int, int]:
        latest_sequence = self._sequence
        history_floor = (
            self._stream_history[0].sequence if self._stream_history else latest_sequence + 1
        )
        return history_floor, latest_sequence

    def _update_reset_locked(self) -> OperatorUpdateEnvelope:
        history_floor, latest_sequence = self._history_bounds_locked()
        return OperatorUpdateEnvelope(
            operator_instance_id=self._operator_instance_id,
            reset_required=ResetRequired(
                history_floor=history_floor,
                latest_sequence=latest_sequence,
            ),
        )

    def unsubscribe_operator_updates(self, subscription: queue.Queue) -> None:
        with self._lock:
            self._update_subscribers = [
                item for item in self._update_subscribers if item is not subscription
            ]

    def close(self) -> None:
        """Stop services boundedly and drain accepted notifications in order."""
        with self._lock:
            first_close = not self._closed
            self._closed = True
            handles = list(self._active_runs.items())
        if first_close:
            self._watcher_stop.set()
            self._result_cleanup_stop.set()
            self._scheduler.stop()
            self._webhooks.close()
            if self._watcher_thread is not None:
                self._watcher_thread.join(timeout=2.0)
            if self._result_cleanup_thread is not None:
                self._result_cleanup_thread.join(timeout=2.0)

        for _, handle in handles:
            handle.cancel_event.set()
            handle.start_event.set()
        deadline = time.monotonic() + self._cancel_grace
        for _, handle in handles:
            if handle.process.pid is not None:
                handle.process.join(timeout=max(0.0, deadline - time.monotonic()))
            _teardown_process_group(handle.process, handle.windows_job)

        delayed_drains = []
        drain_deadline = time.monotonic() + 2.0
        current_thread = threading.current_thread()
        for _, handle in handles:
            preparation = handle.preparation_thread
            if preparation is not None and preparation is not current_thread:
                preparation.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        for run_id, handle in handles:
            drain = handle.drain_thread
            if drain is not None and drain is not current_thread:
                drain.join(timeout=max(0.0, drain_deadline - time.monotonic()))
            if drain is not None and drain.is_alive():
                delayed_drains.append((run_id, handle))
                continue
            with self._lock:
                if self._active_runs.get(run_id) is handle:
                    self._active_runs.pop(run_id, None)
        if first_close:
            self._begin_notification_shutdown(tuple(delayed_drains))

    def _result_cleanup_loop(self) -> None:
        retention = self._result_retention_seconds
        if retention is None:
            return
        interval = min(60.0, max(0.1, retention / 10))
        while not self._result_cleanup_stop.wait(interval):
            self._expire_results()

    def _expire_results(self) -> None:
        retention = self._result_retention_seconds
        if retention is None:
            return
        cutoff = time.monotonic() - retention
        expired: list[tuple[str, StoredWorkflowResult]] = []
        with self._lock:
            for run_id, stored in list(self._stored_results.items()):
                if stored.published_at <= cutoff and self._result_leases.get(run_id, 0) == 0:
                    expired.append((run_id, self._stored_results.pop(run_id)))
        for run_id, stored in expired:
            try:
                self._result_store.discard(stored)
            except (OSError, ValueError):
                logging.getLogger(__name__).exception(
                    "Could not remove expired workflow result %s",
                    run_id,
                )
                with self._lock:
                    if not self._closed and run_id not in self._stored_results:
                        self._stored_results[run_id] = stored

    def _begin_notification_shutdown(
        self,
        delayed_drains: tuple[tuple[str, _RunHandle], ...],
    ) -> None:
        if not delayed_drains:
            self._stop_notification_dispatcher()
            self._close_result_store()
            return
        shutdown_thread = threading.Thread(
            target=self._finish_notification_shutdown,
            args=(delayed_drains,),
            name="avalanche-notification-shutdown",
            daemon=True,
        )
        with self._lock:
            self._notification_shutdown_thread = shutdown_thread
        shutdown_thread.start()

    def _finish_notification_shutdown(
        self,
        delayed_drains: tuple[tuple[str, _RunHandle], ...],
    ) -> None:
        for run_id, handle in delayed_drains:
            drain = handle.drain_thread
            if drain is not None:
                drain.join()
            with self._lock:
                if self._active_runs.get(run_id) is handle:
                    self._active_runs.pop(run_id, None)
        self._stop_notification_dispatcher()
        self._close_result_store()

    def _close_result_store(self) -> None:
        with self._lock:
            self._stored_results.clear()
            self._result_leases.clear()
        self._result_store.close()

    def _start_notification_dispatcher(self) -> None:
        notification_thread = threading.Thread(
            target=self._notification_loop,
            name="avalanche-notifications",
            daemon=True,
        )
        self._notification_thread = notification_thread
        notification_thread.start()

    def _stop_notification_dispatcher(self) -> None:
        with self._lock:
            if self._notification_stop_enqueued:
                return
            self._notification_stop_enqueued = True
            notification_thread = self._notification_thread
            if notification_thread is None or notification_thread.ident is None:
                return
            self._notification_queue.put_nowait(None)
        if threading.current_thread() is not notification_thread:
            notification_thread.join(timeout=2.0)

    def _start_watcher(self) -> None:
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, name="avalanche-watcher", daemon=True
        )
        self._watcher_thread.start()
        if not self._watcher_ready.wait(timeout=2.0):
            self._watcher_stop.set()
            self._watcher_thread.join(timeout=2.0)
            raise RuntimeError("Workflow watcher did not become ready")

    def _watch_loop(self) -> None:
        from watchfiles import watch

        locators = tuple(descriptor.locator for descriptor in self._registry.descriptors())
        source_roots = resolve_watch_roots(self._registry.configured_roots, locators)
        watch_dirs = tuple(str(path) for path in source_roots)
        logger.info("Workflow watcher started: roots=%s", watch_dirs)
        try:
            for changes in watch(
                *watch_dirs,
                stop_event=self._watcher_stop,
                watch_filter=lambda _, path: is_watch_path_included(path, source_roots),
                rust_timeout=50,
                yield_on_timeout=True,
            ):
                self._watcher_ready.set()
                if not changes:
                    continue
                changed_files = tuple(sorted(path for _, path in changes))
                self._refresh_workflows(changed_files)
        finally:
            logger.info("Workflow watcher stopped")

    def _refresh_workflows(self, changed_files: tuple[str, ...] = ()) -> None:
        self._publish_workflow_reload_status(reloading=True)
        try:
            self._reload_workflow_catalog(changed_files)
        finally:
            self._publish_workflow_reload_status(reloading=False)

    def _reload_workflow_catalog(self, changed_files: tuple[str, ...] = ()) -> None:
        previous = self._registry.view
        logger.info(
            "Workflow reload started: revision=%d changed_files=%s",
            previous.revision,
            changed_files,
        )
        # Publishing descriptors and replacing schedules are one logical update.
        # Otherwise an old cron can resolve newly-published same-ID source in the
        # small window between these two operations.
        with self._scheduler.reconciliation_boundary():
            view = (
                self._registry.rescan(changed_files, validate=routes_for)
                if changed_files
                else self._registry.rescan(validate=routes_for)
            )
            failed_diagnostics = tuple(
                diagnostic for diagnostic in view.diagnostics if diagnostic.kind != "skipped"
            )
            if failed_diagnostics:
                logger.warning(
                    "Workflow reload failed; retaining catalog revision %d: %s",
                    previous.revision,
                    _summarize_reload_diagnostics(failed_diagnostics),
                )
            if view is previous:
                if not failed_diagnostics:
                    logger.info(
                        "Workflow reload unchanged: revision=%d",
                        previous.revision,
                    )
                return
            try:
                self._scheduler.reconcile(view.by_id.values())
                self._reconcile_webhooks(view.by_id.values())
            except OSError as exc:
                self._registry.restore_view(view, previous)
                self._scheduler.reconcile(previous.by_id.values())
                self._reconcile_webhooks(previous.by_id.values())
                error = _bound_reload_log_text(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "Workflow reload reconciliation failed; retaining catalog "
                    "revision %d: %s",
                    previous.revision,
                    error,
                )
                return
        self._publish_catalog(view)
        if not failed_diagnostics:
            logger.info(
                "Workflow reload succeeded: revision=%d->%d workflows=%d",
                previous.revision,
                view.revision,
                len(view.by_id),
            )

    def _reconcile_webhooks(self, descriptors) -> None:
        routes = routes_for(tuple(descriptors))
        self._webhooks.reconcile(routes)
        self._webhook_routes = routes

    def _await_prepared(
        self, handle: _RunHandle
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        deadline = time.monotonic() + self._prepare_timeout
        buffered: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                event = handle.event_queue.get(timeout=0.1)
            except queue.Empty:
                if handle.cancel_event.is_set():
                    raise RuntimeError("Run preparation cancelled")
                if not handle.process.is_alive():
                    raise RuntimeError(
                        f"Run coordinator exited during preparation (exit code "
                        f"{handle.process.exitcode})"
                    )
                continue
            event_type = _validate_preparation_event(event)
            if event_type == "prepared":
                return event, buffered
            if event_type == "prepare_failed":
                raise RuntimeError(
                    f"Workflow preparation failed: {event['error']}\n{event['traceback']}"
                )
            buffered.append(event)
        raise TimeoutError(f"Workflow preparation exceeded {self._prepare_timeout:.1f}s")

    @staticmethod
    def _run_from_prepared(
        run_id: str,
        workflow_id: str,
        catalog_display_name: str,
        triggered_by: str,
        triggered_at: float,
        prepared: dict[str, Any],
    ) -> RunState:
        display_name = prepared.get("display_name") or catalog_display_name
        node_ids = tuple(prepared["node_ids"])
        topology = WorkflowTopology(
            node_ids=node_ids,
            graph=tuple(
                (node_id, tuple(prepared["graph"].get(node_id, ()))) for node_id in node_ids
            ),
            node_types=tuple(
                (node_id, prepared["node_types"][node_id]) for node_id in node_ids
            ),
            display_names=tuple(
                (node_id, prepared["display_names"][node_id]) for node_id in node_ids
            ),
            agent_field_schemas_json=tuple(
                (node_id, prepared["agent_field_schemas_json"][node_id])
                for node_id in node_ids
                if node_id in prepared["agent_field_schemas_json"]
            ),
            agent_instruction_lines=tuple(
                (node_id, prepared["agent_instruction_lines"][node_id])
                for node_id in node_ids
                if node_id in prepared["agent_instruction_lines"]
            ),
            standard_step_docstring_lines=tuple(
                (node_id, prepared["standard_step_docstring_lines"][node_id])
                for node_id in node_ids
                if node_id in prepared["standard_step_docstring_lines"]
            ),
        )
        run = RunState(
            run_id=run_id,
            flow_name=display_name,
            workflow_id=workflow_id,
            workflow_display_name=display_name,
            topology=topology,
            status=RunStatus.PENDING,
            triggered_by=triggered_by,
            triggered_at=triggered_at,
        )
        for node_id in node_ids:
            run.nodes[node_id] = NodeState(
                node_id=node_id,
                name=prepared["display_names"][node_id],
                node_type=prepared["node_types"][node_id],
            )
        return run

    def _drain_run_events(
        self,
        run_id: str,
        handle: _RunHandle,
        buffered_events: list[dict[str, Any]],
    ) -> None:
        handle.publication_event.wait()
        terminal = False
        pending = list(buffered_events)
        dead_polls = 0
        try:
            while not terminal:
                if pending:
                    event = pending.pop(0)
                else:
                    try:
                        event = handle.event_queue.get(timeout=0.1)
                        dead_polls = 0
                    except queue.Empty:
                        if handle.process.is_alive():
                            continue
                        dead_polls += 1
                        if dead_polls < 3:
                            continue
                        self._finish_exited_run(run_id, handle)
                        break
                try:
                    if _is_provisional_success_event(event):
                        _validate_run_event(event)
                        try:
                            quiesced = _teardown_process_group(
                                handle.process,
                                handle.windows_job,
                                wait_before_term=2.0,
                            )
                        except Exception as exc:
                            raise _CoordinatorProtocolError(
                                "worker process-group quiescence failed"
                            ) from exc
                        if not quiesced:
                            raise _CoordinatorProtocolError(
                                "worker process group could not be quiesced"
                            )
                        natural_exitcode = getattr(
                            quiesced,
                            "natural_exitcode",
                            None,
                        )
                        if natural_exitcode not in {None, 0}:
                            raise _CoordinatorProtocolError(
                                "coordinator exited unsuccessfully after provisional success"
                            )
                        handle.success_quiesced = True
                        if not handle.cancel_event.is_set():
                            late_event = _event_after_provisional_success(handle.event_queue)
                            if late_event is not None:
                                raise _CoordinatorProtocolError(
                                    "coordinator emitted an event after provisional success"
                                )
                    terminal = self._apply_event(run_id, handle, event)
                except _CoordinatorProtocolError as exc:
                    self._finish_protocol_fault(run_id, handle, event, exc)
                    break
        finally:
            try:
                _teardown_process_group(
                    handle.process,
                    handle.windows_job,
                    wait_before_term=2.0,
                )
            except Exception:
                logger.exception("Run coordinator cleanup failed for %s", run_id)
            finally:
                with self._lock:
                    self._active_runs.pop(run_id, None)
                _close_event_queue(handle.event_queue)

    def _apply_event(
        self,
        run_id: str,
        handle: _RunHandle,
        event: dict[str, Any],
    ) -> bool:
        event_type = _validate_run_event(event, validate_result=False)
        terminal = event_type == "terminal"
        result_manifest_sha256 = (
            _result_manifest_digest_from_event(event)
            if terminal and event["status"] == "success"
            else None
        )
        if result_manifest_sha256 is not None and not handle.success_quiesced:
            raise _CoordinatorProtocolError(
                "workflow success was not quiesced before result validation"
            )
        cancelled_result = result_manifest_sha256 is not None and handle.cancel_event.is_set()
        try:
            stored_result = (
                self._result_store.accept(
                    handle.result_bundle,
                    result_manifest_sha256,
                    cancel_signal=handle.cancel_event,
                )
                if result_manifest_sha256 is not None and not cancelled_result
                else None
            )
        except ResultPublicationCancelledError:
            cancelled_result = True
            stored_result = None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _CoordinatorProtocolError(
                f"invalid workflow result publication: {exc}"
            ) from exc
        if terminal and stored_result is None:
            self._result_store.discard(handle.result_bundle)

        discard_stored_result = False
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                discard_stored_result = stored_result is not None
            else:
                summary_changed = False
                changed_node_ids: tuple[str, ...] = ()
                status_node_ids: tuple[str, ...] | None = None
                trace_node_ids: tuple[str, ...] = ()
                finalized_traces: dict[str, bytes] = {}
                agent_events: dict[str, AgentEvent] = {}
                log_entry: LogEntry | None = None
                mutated = False

                if event_type == "running":
                    if run.status != RunStatus.CANCELLED:
                        run.status = RunStatus.RUNNING
                        run.started_at = event["timestamp"]
                        summary_changed = True
                        mutated = True
                elif event_type.startswith("node_"):
                    node = run.nodes.get(event["node_id"])
                    if node is None:
                        raise _CoordinatorProtocolError(
                            "node event references unpublished node "
                            f"{_bounded_ascii(event['node_id'])}"
                        )
                    if run.status != RunStatus.CANCELLED:
                        status = {
                            "node_started": NodeStatus.RUNNING,
                            "node_succeeded": NodeStatus.SUCCESS,
                            "node_failed": NodeStatus.FAILED,
                        }[event_type]
                        node.status = status
                        if status == NodeStatus.RUNNING:
                            node.started_at = event["timestamp"]
                        else:
                            node.ended_at = event["timestamp"]
                            node.error = event["error"] if status == NodeStatus.FAILED else None
                        changed_node_ids = (node.node_id,)
                        status_node_ids = changed_node_ids
                        mutated = True
                elif event_type == "agent_evidence":
                    node_id = event["node_id"]
                    if node_id not in run.nodes:
                        raise _CoordinatorProtocolError(
                            "agent evidence references unpublished node "
                            f"{_bounded_ascii(node_id)}"
                        )
                    try:
                        mutation = self._record_agent_evidence_event_locked(
                            run, node_id, event["event"]
                        )
                    except BaseException:
                        mutation = None
                    if mutation is not None:
                        log_entry = mutation.entry
                        changed_node_ids = (node_id,)
                        trace_node_ids = (node_id,)
                        status_node_ids = ()
                        if mutation.agent_event is not None:
                            agent_events[node_id] = mutation.agent_event
                        if mutation.finalized_trace is not None:
                            finalized_traces[node_id] = mutation.finalized_trace
                        mutated = True
                elif event_type == "log":
                    if run.status in {
                        RunStatus.SUCCESS,
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                    }:
                        return False
                    log_node_id = event["node_id"]
                    if log_node_id not in run.nodes:
                        matches = (
                            node.node_id
                            for node in run.nodes.values()
                            if node.name == log_node_id
                        )
                        matched_node_id = next(matches, None)
                        if matched_node_id is not None and next(matches, None) is None:
                            log_node_id = matched_node_id
                    log_entry = LogEntry(
                        timestamp=datetime.fromtimestamp(event["timestamp"]),
                        level=_LEVEL_MAP.get(event["level"], LogLevel.INFO),
                        node_id=log_node_id,
                        message=event["message"],
                    )
                    self._append_log_locked(run, log_entry)
                    mutated = True
                elif terminal:
                    if run.status != RunStatus.CANCELLED:
                        effective_status = (
                            "cancelled"
                            if event["status"] == "success"
                            and (cancelled_result or handle.cancel_event.is_set())
                            else event["status"]
                        )
                        run.status = {
                            "success": RunStatus.SUCCESS,
                            "failed": RunStatus.FAILED,
                            "cancelled": RunStatus.CANCELLED,
                        }[effective_status]
                        if stored_result is not None and effective_status == "success":
                            self._stored_results[run_id] = stored_result
                        elif stored_result is not None:
                            discard_stored_result = True
                    elif stored_result is not None:
                        discard_stored_result = True
                    run.ended_at = time.monotonic()
                    changed_node_ids = self._skip_unfinished_nodes_locked(run)
                    status_node_ids = changed_node_ids
                    summary_changed = True
                    mutated = True

                if not mutated:
                    return terminal
                notifications = self._publish_run_locked(
                    run,
                    summary_changed=summary_changed,
                    changed_node_ids=changed_node_ids,
                    status_node_ids=status_node_ids,
                    trace_node_ids=trace_node_ids,
                    finalized_traces=finalized_traces,
                    agent_events=agent_events,
                    log_entry=log_entry,
                )

        if discard_stored_result and stored_result is not None:
            self._result_store.discard(stored_result)
        if run is None:
            return terminal
        self._wait_for_notifications(notifications)
        return terminal

    def _record_agent_evidence_event(
        self,
        run: RunState,
        node_id: str,
        event: dict[str, Any],
    ) -> LogEntry | None:
        """Atomically project one best-effort agent event and publish its watermark."""
        try:
            with self._lock:
                mutation = self._record_agent_evidence_event_locked(run, node_id, event)
                if mutation is None:
                    return None
                finalized_traces = (
                    {node_id: mutation.finalized_trace}
                    if mutation.finalized_trace is not None
                    else {}
                )
                notifications = self._publish_run_locked(
                    run,
                    summary_changed=False,
                    changed_node_ids=(node_id,),
                    status_node_ids=(),
                    trace_node_ids=(node_id,),
                    finalized_traces=finalized_traces,
                    agent_events=(
                        {node_id: mutation.agent_event}
                        if mutation.agent_event is not None
                        else {}
                    ),
                    log_entry=mutation.entry,
                )
            self._wait_for_notifications(notifications)
            return mutation.entry
        except BaseException:
            return None

    def _record_agent_evidence_event_locked(
        self,
        run: RunState,
        node_id: str,
        event: dict[str, Any],
    ) -> _AgentEvidenceMutation | None:
        if node_id not in run.nodes or not isinstance(event, dict):
            return None

        key = (run.run_id, node_id)
        invocation_id = event.get("invocation_id")
        if (
            not isinstance(invocation_id, str)
            or not invocation_id
            or len(invocation_id) > _MAX_EVENT_FIELD_LENGTH
        ):
            return None
        projected_events = self._agent_events.setdefault(key, [])
        previous_descriptor = self._trace_descriptors.get(
            key, TraceDescriptor(status="in_progress")
        )
        finalized_trace: bytes | None = None
        projected_agent_event: AgentEvent | None = None
        invocation_sequence_key: tuple[str, str, str] | None = None
        kind = event.get("kind")
        level = LogLevel.INFO
        error = self._trace_errors.get(key)
        if kind == "evidence":
            sequence = event.get("sequence")
            event_kind = event.get("event_kind")
            timestamp_ns = event.get("timestamp_ns")
            data = event.get("data", {})
            if (
                not isinstance(sequence, int)
                or sequence < 1
                or not isinstance(event_kind, str)
                or not isinstance(timestamp_ns, int)
                or not isinstance(data, dict)
            ):
                return None
            invocation_sequence_key = (run.run_id, node_id, invocation_id)
            if sequence <= self._agent_invocation_sequences.get(invocation_sequence_key, 0):
                return None
            projected = {
                "sequence": sequence,
                "event_kind": event_kind,
                "timestamp_ns": timestamp_ns,
                "data": data,
                "invocation_id": invocation_id,
            }
            _validate_agent_detail_depth(projected)
            event_json = json.dumps(
                projected,
                default=str,
                separators=(",", ":"),
            )
            event_size = len(event_json.encode())
            if event_size > self._max_agent_event_bytes:
                raise _CoordinatorProtocolError(
                    f"agent event exceeds {self._max_agent_event_bytes} byte limit"
                )
            iteration = data.get("iteration")
            duration_ms = data.get("duration_ms")
            tool_count = data.get("tool_count")
            predict_count = data.get("predict_count")
            projected_agent_event = AgentEvent(
                invocation_id=invocation_id,
                event_sequence=len(projected_events) + 1,
                event_json=event_json,
                size_bytes=event_size,
                event_kind=event_kind,
                iteration=iteration if isinstance(iteration, int) else None,
                duration_ms=duration_ms if isinstance(duration_ms, int) else None,
                error=bool(data.get("error")),
                tool_count=tool_count if isinstance(tool_count, int) else 0,
                predict_count=predict_count if isinstance(predict_count, int) else 0,
            )
            detail = []
            if data.get("iteration") is not None:
                detail.append(f"iteration={data['iteration']}")
            if data.get("duration_ms") is not None:
                detail.append(f"duration={data['duration_ms']}ms")
            if data.get("error"):
                detail.append(f"error={data['error']}")
            message = f"Agent {event_kind}"
            if detail:
                message += " " + " ".join(detail)
            status = previous_descriptor.status
            if data.get("error") or event_kind in {"run.failed", "run.cancelled"}:
                level = LogLevel.ERROR
                status = "error"
                error = str(data.get("error") or event_kind)
            descriptor = replace(
                previous_descriptor,
                status=status,
                event_count=len(projected_events) + 1,
                latest_event_sequence=len(projected_events) + 1,
            )
        elif kind == "trace_finished":
            trace = event.get("trace")
            if not isinstance(trace, dict):
                return None
            _validate_agent_detail_depth(trace)
            header = _trace_header_from_payload(trace)
            trace_header = {
                name: value
                for name, value in trace.items()
                if name not in {"steps", "evidence"}
            }
            evidence = trace.get("evidence")
            if isinstance(evidence, dict):
                trace_header["evidence"] = {
                    name: value for name, value in evidence.items() if name != "events"
                }
            finalized_trace = json.dumps(
                trace_header,
                default=str,
                separators=(",", ":"),
            ).encode()
            if len(finalized_trace) > self._max_trace_body_bytes:
                raise _CoordinatorProtocolError(
                    f"agent trace header exceeds {self._max_trace_body_bytes} byte limit"
                )
            versions = self._trace_bodies.get(key, {})
            if (
                previous_descriptor.available
                and self._trace_invocation_ids.get(key) == invocation_id
                and versions.get(previous_descriptor.revision) == finalized_trace
            ):
                return None
            status = str(trace.get("status") or "unavailable")[:80]
            error = None
            descriptor = TraceDescriptor(
                status=status,
                revision=previous_descriptor.revision,
                available=True,
                complete=bool(isinstance(evidence, dict) and evidence.get("complete")),
                event_count=len(projected_events),
                size_bytes=len(finalized_trace),
                latest_event_sequence=(
                    projected_events[-1].event_sequence if projected_events else 0
                ),
                header=header,
            )
            message = f"Agent trace {status}"
            if status == "error":
                level = LogLevel.ERROR
        elif kind == "trace_unavailable":
            error = str(event.get("error") or "Agent trace unavailable")
            descriptor = TraceDescriptor(
                status="unavailable",
                revision=previous_descriptor.revision,
                available=False,
                complete=False,
                event_count=len(projected_events),
                latest_event_sequence=(
                    projected_events[-1].event_sequence if projected_events else 0
                ),
            )
            message = f"Agent trace unavailable: {error}"
            level = LogLevel.WARN
        else:
            return None

        entry = LogEntry(
            timestamp=datetime.now(),
            level=level,
            node_id=node_id,
            message=message,
        )
        body_size = (
            projected_agent_event.size_bytes
            if projected_agent_event is not None
            else len(finalized_trace or b"")
        )
        log_size = len(entry.message.encode())
        self._ensure_detail_capacity_locked(
            run.run_id,
            node_id,
            log_bytes=log_size,
            log_entries=1,
            node_bytes=body_size,
        )
        if projected_agent_event is not None:
            projected_events.append(projected_agent_event)
            assert invocation_sequence_key is not None
            self._agent_invocation_sequences[invocation_sequence_key] = sequence
        if kind != "evidence":
            self._trace_invocation_ids[key] = invocation_id
        self._trace_descriptors[key] = descriptor
        self._trace_errors[key] = error
        self._append_log_unchecked_locked(run, entry, log_size)
        if body_size:
            self._node_detail_bytes[key] = self._node_detail_bytes.get(key, 0) + body_size
            self._run_detail_bytes[run.run_id] = (
                self._run_detail_bytes.get(run.run_id, 0) + body_size
            )
        return _AgentEvidenceMutation(
            entry=entry,
            agent_event=projected_agent_event,
            finalized_trace=finalized_trace,
        )

    def _ensure_detail_capacity_locked(
        self,
        run_id: str,
        node_id: str,
        *,
        log_bytes: int = 0,
        log_entries: int = 0,
        node_bytes: int = 0,
    ) -> None:
        if len(self._logs.get(run_id, ())) + log_entries > self._max_run_log_entries:
            raise _CoordinatorProtocolError(
                f"run logs exceed {self._max_run_log_entries} entry limit"
            )
        if self._run_log_bytes.get(run_id, 0) + log_bytes > self._max_run_log_bytes:
            raise _CoordinatorProtocolError(
                f"run logs exceed {self._max_run_log_bytes} byte limit"
            )
        key = (run_id, node_id)
        if self._node_detail_bytes.get(key, 0) + node_bytes > self._max_node_detail_bytes:
            raise _CoordinatorProtocolError(
                f"node detail exceeds {self._max_node_detail_bytes} byte limit"
            )
        if (
            self._run_detail_bytes.get(run_id, 0) + log_bytes + node_bytes
            > self._max_run_detail_bytes
        ):
            raise _CoordinatorProtocolError(
                f"run detail exceeds {self._max_run_detail_bytes} byte limit"
            )

    def _append_log_unchecked_locked(
        self,
        run: RunState,
        entry: LogEntry,
        size_bytes: int,
    ) -> None:
        logs = self._logs.setdefault(run.run_id, [])
        sequence = len(logs) + 1
        logs.append(
            SequencedLogEntry(
                sequence=sequence,
                entry=deepcopy(entry),
                size_bytes=size_bytes,
            )
        )
        node_sequences = self._log_sequences_by_node.setdefault(run.run_id, {})
        node_sequences.setdefault(entry.node_id, []).append(sequence)
        self._run_log_bytes[run.run_id] = self._run_log_bytes.get(run.run_id, 0) + size_bytes
        self._run_detail_bytes[run.run_id] = (
            self._run_detail_bytes.get(run.run_id, 0) + size_bytes
        )

    def _append_log_locked(self, run: RunState, entry: LogEntry) -> None:
        size_bytes = len(entry.message.encode())
        self._ensure_detail_capacity_locked(
            run.run_id,
            entry.node_id,
            log_bytes=size_bytes,
            log_entries=1,
        )
        self._append_log_unchecked_locked(run, entry, size_bytes)

    def _finish_protocol_fault(
        self,
        run_id: str,
        handle: _RunHandle,
        event: object,
        exc: _CoordinatorProtocolError,
    ) -> None:
        entry = LogEntry(
            timestamp=datetime.now(),
            level=LogLevel.ERROR,
            node_id="operator",
            message=_protocol_fault_message(event, exc),
        )
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in {
                RunStatus.SUCCESS,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                notifications = None
            else:
                run.status = (
                    RunStatus.CANCELLED if handle.cancel_event.is_set() else RunStatus.FAILED
                )
                self._stored_results.pop(run_id, None)
                run.ended_at = time.monotonic()
                self._append_log_locked(run, entry)
                changed_node_ids = self._skip_unfinished_nodes_locked(run)
                notifications = self._publish_run_locked(
                    run,
                    changed_node_ids=changed_node_ids,
                    status_node_ids=changed_node_ids,
                    log_entry=entry,
                )
        self._result_store.discard(handle.result_bundle)
        if notifications is not None:
            self._wait_for_notifications(notifications)

    def _finish_exited_run(self, run_id: str, handle: _RunHandle) -> None:
        cancelled = handle.cancel_event.is_set()
        entry = LogEntry(
            timestamp=datetime.now(),
            level=LogLevel.ERROR,
            node_id="operator",
            message=(
                "Run coordinator was terminated after cancellation"
                if cancelled
                else "Run coordinator exited without a terminal event "
                f"(exit code {handle.process.exitcode})"
            ),
        )
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                self._result_store.discard(handle.result_bundle)
                return
            run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
            self._stored_results.pop(run_id, None)
            run.ended_at = time.monotonic()
            log_entry = None
            if not cancelled:
                self._append_log_locked(run, entry)
                log_entry = entry
            changed_node_ids = self._skip_unfinished_nodes_locked(run)
            notifications = self._publish_run_locked(
                run,
                changed_node_ids=changed_node_ids,
                status_node_ids=changed_node_ids,
                log_entry=log_entry,
            )
        self._result_store.discard(handle.result_bundle)
        self._wait_for_notifications(notifications)

    def _force_cancel_after_grace(self, run_id: str, handle: _RunHandle) -> None:
        handle.process.join(timeout=self._cancel_grace)
        with self._lock:
            if self._active_runs.get(run_id) is not handle:
                return
        _teardown_process_group(handle.process, handle.windows_job)

    @staticmethod
    def _skip_unfinished_nodes_locked(run: RunState) -> tuple[str, ...]:
        changed = []
        for node in run.nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                node.status = NodeStatus.SKIPPED
                changed.append(node.node_id)
        return tuple(changed)

    def _publish_run_locked(
        self,
        run: RunState,
        *,
        summary_changed: bool = True,
        changed_node_ids: tuple[str, ...] = (),
        status_node_ids: tuple[str, ...] | None = None,
        trace_node_ids: tuple[str, ...] = (),
        finalized_traces: Mapping[str, bytes] | None = None,
        agent_events: Mapping[str, AgentEvent] | None = None,
        log_entry: LogEntry | None = None,
    ) -> _RunNotifications:
        """Atomically publish one mutation to structural, detail, and update state."""
        if self._notification_stop_enqueued:
            raise RuntimeError("Operator notification dispatcher is closed")

        is_new = run.run_id not in self._run_created_sequences
        status_node_ids = changed_node_ids if status_node_ids is None else status_node_ids
        status_node_ids = tuple(dict.fromkeys(status_node_ids))
        trace_node_ids = tuple(dict.fromkeys(trace_node_ids))
        agent_events = agent_events or {}
        finalized_traces = finalized_traces or {}
        activity_count = int(log_entry is not None) + len(agent_events) + len(trace_node_ids)
        terminal_seal_appended = False
        if (
            run.status
            in {
                RunStatus.SUCCESS,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
            and run.terminal_seal is None
        ):
            terminal_sequence = self._run_activity_sequences.get(run.run_id, 0)
            terminal_sequence += activity_count + 1
            run.terminal_seal = TerminalSealDescriptor(
                activity_id=f"terminal_seal:{run.run_id}:{terminal_sequence}",
                run_sequence=terminal_sequence,
                timestamp=datetime.now(),
                terminal_status=run.status,
            )
            terminal_seal_appended = True
        activity_count += int(terminal_seal_appended)
        self._run_activity_sequences[run.run_id] = (
            self._run_activity_sequences.get(run.run_id, 0) + activity_count
        )

        if is_new:
            update_count = 1 + int(terminal_seal_appended)
        else:
            update_count = (
                int(summary_changed)
                + len(status_node_ids)
                + int(log_entry is not None)
                + len(agent_events)
                + len(trace_node_ids)
                + int(terminal_seal_appended)
            )
            if update_count == 0:
                summary_changed = True
                update_count = 1

        first_sequence = self._sequence + 1
        publication_sequence = self._sequence + update_count
        self._run_created_sequences.setdefault(run.run_id, first_sequence)
        if summary_changed or is_new:
            self._run_revisions[run.run_id] = publication_sequence
        if is_new:
            for node_id in run.nodes:
                self._node_revisions[(run.run_id, node_id)] = publication_sequence
        for node_id in changed_node_ids:
            self._node_revisions[(run.run_id, node_id)] = publication_sequence

        for node_id in trace_node_ids:
            key = (run.run_id, node_id)
            descriptor = self._trace_descriptors[key]
            versions = self._trace_bodies.setdefault(key, {})
            finalized_trace = finalized_traces.get(node_id)
            if finalized_trace is not None:
                versions[publication_sequence] = finalized_trace
            elif descriptor.available and descriptor.revision in versions:
                versions[publication_sequence] = versions[descriptor.revision]
            self._trace_descriptors[key] = replace(
                descriptor,
                revision=publication_sequence,
            )

        changes: list[Any] = []
        if is_new:
            summary = self._run_summary_locked(run)
            snapshot = self._run_snapshot_locked(
                run,
                summary=summary,
                as_of_sequence=publication_sequence,
            )
            changes.append(
                RunCreated(summary=summary, nodes=snapshot.nodes, topology=snapshot.topology)
            )
        else:
            if summary_changed:
                changes.append(
                    RunStatusChanged(
                        run_id=run.run_id,
                        status=run.status,
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                        revision=publication_sequence,
                    )
                )
            for node_id in status_node_ids:
                node = run.nodes[node_id]
                changes.append(
                    NodeStatusChanged(
                        run_id=run.run_id,
                        node_id=node_id,
                        status=node.status,
                        started_at=node.started_at,
                        ended_at=node.ended_at,
                        running_elapsed_seconds=(
                            node.elapsed if node.status is NodeStatus.RUNNING else None
                        ),
                        revision=publication_sequence,
                        error=node.error,
                    )
                )
            if log_entry is not None:
                log_item = self._logs[run.run_id][-1]
                changes.append(
                    LogAppended(
                        run_id=run.run_id,
                        log=self._log_descriptor_locked(
                            run.run_id,
                            log_item,
                            as_of_sequence=publication_sequence,
                        ),
                    )
                )
            for node_id, event in agent_events.items():
                changes.append(
                    AgentEventAppended(
                        run_id=run.run_id,
                        node_id=node_id,
                        event=self._agent_event_descriptor_locked(
                            run.run_id,
                            node_id,
                            event,
                            as_of_sequence=publication_sequence,
                        ),
                    )
                )
            for node_id in trace_node_ids:
                changes.append(
                    TraceFinalized(
                        run_id=run.run_id,
                        node_id=node_id,
                        trace=self._trace_descriptors[(run.run_id, node_id)],
                    )
                )
        if terminal_seal_appended:
            if run.terminal_seal is None:
                raise RuntimeError("terminal seal was not retained before publication")
            changes.append(
                TerminalSealAppended(
                    run_id=run.run_id,
                    seal=run.terminal_seal,
                )
            )

        updates = []
        for change in changes:
            self._sequence += 1
            update = OperatorUpdate(sequence=self._sequence, change=change)
            self._stream_history.append(update)
            updates.append(update)
        if self._sequence != publication_sequence:
            raise RuntimeError("Run publication produced an inconsistent update batch")

        envelopes = tuple(
            OperatorUpdateEnvelope(
                operator_instance_id=self._operator_instance_id,
                update=update,
            )
            for update in updates
        )
        structural_run = deepcopy(run)
        structural_run.logs = []
        structural_run.details_hydrated = False
        for node in structural_run.nodes.values():
            node.agent_trace_json = None
        detail_updates: list[DetailUpdate] = []
        for update in updates:
            change = update.change
            if isinstance(change, LogAppended) and log_entry is not None:
                detail_updates.append(
                    LogDetailAppended(
                        operator_instance_id=self._operator_instance_id,
                        run_id=run.run_id,
                        created_sequence=self._run_created_sequences[run.run_id],
                        sequence=update.sequence,
                        log_sequence=change.log.sequence,
                        log=deepcopy(log_entry),
                    )
                )
            elif isinstance(change, AgentEventAppended):
                detail_updates.append(
                    AgentEventDetailAppended(
                        operator_instance_id=self._operator_instance_id,
                        run_id=run.run_id,
                        created_sequence=self._run_created_sequences[run.run_id],
                        sequence=update.sequence,
                        node_id=change.node_id,
                        event=deepcopy(agent_events[change.node_id]),
                    )
                )
        notifications = _RunNotifications(
            sequence=publication_sequence,
            run_callbacks=tuple(
                (callback, deepcopy(structural_run)) for callback in self._run_callbacks
            ),
            detail_callbacks=tuple(
                (callback, deepcopy(detail))
                for detail in detail_updates
                for callback in self._detail_callbacks
            ),
            log_callbacks=(
                tuple((callback, deepcopy(log_entry)) for callback in self._log_callbacks)
                if log_entry is not None
                else ()
            ),
            update_subscribers=tuple(
                (subscription, envelopes) for subscription in self._update_subscribers
            ),
            ready=threading.Event(),
            delivered=threading.Event(),
        )
        self._notification_queue.put_nowait(notifications)
        return notifications

    def _notify_run(self, run: RunState, *, summary_changed: bool = True) -> None:
        with self._lock:
            notifications = self._publish_run_locked(
                run,
                summary_changed=summary_changed,
            )
        self._wait_for_notifications(notifications)

    def _publish_catalog(self, view: CatalogView) -> None:
        with self._lock:
            self._sequence += 1
            self._catalog_sequence = self._sequence
            catalog = self._catalog_snapshot(view, self._catalog_sequence)
            update = OperatorUpdate(
                sequence=self._sequence,
                change=CatalogReplaced(catalog=catalog),
            )
            self._stream_history.append(update)
            envelope = OperatorUpdateEnvelope(
                operator_instance_id=self._operator_instance_id,
                update=update,
            )
            notifications = _RunNotifications(
                sequence=self._sequence,
                run_callbacks=(),
                detail_callbacks=(),
                log_callbacks=(),
                update_subscribers=tuple(
                    (subscription, (envelope,)) for subscription in self._update_subscribers
                ),
                ready=threading.Event(),
                delivered=threading.Event(),
            )
            self._notification_queue.put_nowait(notifications)
        self._wait_for_notifications(notifications)

    def publish_update(self, change: OperatorUpdateChange) -> None:
        """Publish one caller-owned typed lifecycle update."""
        with self._lock:
            self._sequence += 1
            if isinstance(change, CatalogReloadRequired):
                # A reload notice fences the existing catalog graph at this event.
                # DiscoverFlows can therefore return a baseline that covers the notice
                # without publishing a replacement catalog or changing its contents.
                self._catalog_sequence = self._sequence
            update = OperatorUpdate(
                sequence=self._sequence,
                change=change,
            )
            self._stream_history.append(update)
            envelope = OperatorUpdateEnvelope(
                operator_instance_id=self._operator_instance_id,
                update=update,
            )
            notifications = _RunNotifications(
                sequence=self._sequence,
                run_callbacks=(),
                detail_callbacks=(),
                log_callbacks=(),
                update_subscribers=tuple(
                    (subscription, (envelope,)) for subscription in self._update_subscribers
                ),
                ready=threading.Event(),
                delivered=threading.Event(),
            )
            self._notification_queue.put_nowait(notifications)
        self._wait_for_notifications(notifications)

    def _publish_workflow_reload_status(self, *, reloading: bool) -> None:
        self.publish_update(WorkflowReloadStatus(reloading=reloading))

    def _wait_for_notifications(self, notifications: _RunNotifications) -> None:
        notifications.ready.set()
        if threading.current_thread() is self._notification_thread:
            return
        notifications.delivered.wait()

    def _notification_loop(self) -> None:
        while True:
            notifications = self._notification_queue.get()
            if notifications is None:
                return
            notifications.ready.wait()
            try:
                self._deliver_notifications(notifications)
            finally:
                notifications.delivered.set()

    def _deliver_notifications(self, notifications: _RunNotifications) -> None:
        for callback, entry in notifications.log_callbacks:
            try:
                callback(entry)
            except Exception:
                pass
        for callback, snapshot in notifications.run_callbacks:
            try:
                callback(deepcopy(snapshot))
            except Exception:
                pass
        for callback, detail in notifications.detail_callbacks:
            try:
                callback(detail)
            except Exception:
                pass
        for subscription, envelopes in notifications.update_subscribers:
            self._deliver_update_batch(subscription, envelopes)

    def _deliver_update_batch(
        self,
        subscription: queue.Queue,
        envelopes: tuple[OperatorUpdateEnvelope, ...],
    ) -> None:
        with self._lock:
            if subscription not in self._update_subscribers:
                return
            for envelope in envelopes:
                try:
                    subscription.put_nowait(envelope)
                except queue.Full:
                    _replace_queue_contents(subscription, self._update_reset_locked())
                    self._update_subscribers = [
                        item for item in self._update_subscribers if item is not subscription
                    ]
                    return


def _bounded_page_size(page_size: int) -> int:
    if page_size < 0:
        raise ValueError("Page size must be non-negative")
    return min(page_size or DETAIL_PAGE_SIZE, MAX_DETAIL_PAGE_SIZE)


def _validated_descriptor_page_order(order: int) -> int:
    if type(order) is not int or order not in {
        _DESCRIPTOR_PAGE_ORDER_FORWARD,
        _DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST,
    }:
        raise ValueError("Invalid descriptor page order")
    return order


def _validated_descriptor_cursor(cursor: int, field_name: str) -> int:
    if type(cursor) is not int or cursor < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return cursor


def _encode_page_token(
    *,
    operator_instance_id: str,
    as_of_sequence: int,
    workflow_selector: str,
    resolved_workflow_id: str,
    created_sequence: int,
    run_id: str,
) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "operator_instance_id": operator_instance_id,
            "as_of_sequence": as_of_sequence,
            "workflow_selector": workflow_selector,
            "resolved_workflow_id": resolved_workflow_id,
            "created_sequence": created_sequence,
            "run_id": run_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_page_token(page_token: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(page_token) % 4)
        value = json.loads(base64.urlsafe_b64decode(page_token + padding))
    except (ValueError, TypeError):
        raise ValueError("Invalid page token") from None
    if not isinstance(value, dict) or value.get("v") != 2:
        raise ValueError("Invalid page token")
    expected_types = {
        "operator_instance_id": str,
        "as_of_sequence": int,
        "workflow_selector": str,
        "resolved_workflow_id": str,
        "created_sequence": int,
        "run_id": str,
    }
    if any(
        type(value.get(field)) is not expected for field, expected in expected_types.items()
    ):
        raise ValueError("Invalid page token")
    return value


def _encode_transport_token(**fields: Any) -> str:
    payload = json.dumps(
        {"v": 1, **{key: value for key, value in fields.items() if key != "v"}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_transport_token(
    token: str,
    expected_kind: str | set[str],
) -> dict[str, Any]:
    try:
        padding = "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(token + padding))
    except (ValueError, TypeError):
        raise ValueError("Invalid detail token") from None
    kinds = {expected_kind} if isinstance(expected_kind, str) else expected_kind
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or value.get("kind") not in kinds
        or type(value.get("operator_instance_id")) is not str
        or type(value.get("as_of_sequence")) is not int
        or type(value.get("run_id")) is not str
    ):
        raise ValueError("Invalid detail token")
    if value["as_of_sequence"] < 0:
        raise ValueError("Invalid detail token")
    if value["kind"] in {"logs", "events"}:
        if type(value.get("through_sequence")) is not int or value["through_sequence"] < 0:
            raise ValueError("Invalid detail token")
        if "cursor" in value:
            if (
                type(value["cursor"]) is not int
                or value["cursor"] < 0
                or type(value.get("order")) is not int
                or value["order"]
                not in {
                    _DESCRIPTOR_PAGE_ORDER_FORWARD,
                    _DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST,
                }
            ):
                raise ValueError("Invalid detail token")
            if value["kind"] == "logs" and type(value.get("node_id")) is not str:
                raise ValueError("Invalid detail token")
    else:
        if type(value.get("sequence")) is not int or value["sequence"] < 1:
            raise ValueError("Invalid detail token")
    if value["kind"] in {"events", "event-body"} and type(value.get("node_id")) is not str:
        raise ValueError("Invalid detail token")
    return value


def _descriptor_wire_size(item: LogRecordDescriptor | AgentEventDescriptor) -> int:
    size = len(item.body_token.encode()) + 64
    if isinstance(item, LogRecordDescriptor):
        size += len(item.node_id.encode()) + len(item.level.value)
    return size


def _take_bounded_descriptors(
    candidates: list[LogRecordDescriptor] | list[AgentEventDescriptor],
    page_size: int,
) -> list[Any]:
    selected = []
    serialized_bytes = 128
    for item in candidates[:page_size]:
        item_bytes = _descriptor_wire_size(item)
        if serialized_bytes + item_bytes > MAX_TRANSPORT_PAGE_BYTES:
            break
        selected.append(item)
        serialized_bytes += item_bytes
    if candidates and not selected:
        raise ValueError("Detail metadata exceeds the transport page byte budget")
    return selected


def _summary_wire_size(summary: RunSummary) -> int:
    return (
        len(summary.run_id.encode())
        + len(summary.flow_name.encode())
        + len(summary.triggered_by.encode())
        + len(summary.workflow_id.encode())
        + len(summary.workflow_display_name.encode())
        + 96
    )


def _take_bounded_summaries(
    candidates: tuple[RunSummary, ...],
    page_size: int,
) -> list[RunSummary]:
    selected = []
    serialized_bytes = 128
    for item in candidates[:page_size]:
        item_bytes = _summary_wire_size(item)
        if serialized_bytes + item_bytes > MAX_TRANSPORT_PAGE_BYTES:
            break
        selected.append(item)
        serialized_bytes += item_bytes
    if candidates and not selected:
        raise ValueError("Run summary exceeds the transport page byte budget")
    return selected


def _materialize_run_detail(capture: _RunDetailCapture) -> RunState:
    run = capture.run
    run.logs = [deepcopy(item.entry) for item in capture.logs]
    for node_id, node in run.nodes.items():
        trace_invocation_id = capture.trace_invocation_ids.get(node_id)
        projected_events = []
        for event in capture.events.get(node_id, ()):
            if trace_invocation_id and event.invocation_id != trace_invocation_id:
                continue
            try:
                projected = json.loads(event.event_json)
            except (TypeError, ValueError):
                continue
            if isinstance(projected, dict):
                projected_events.append(projected)
                projected["invocation_id"] = event.invocation_id
        descriptor = node.trace
        trace = None
        body = capture.trace_bodies.get(node_id)
        if body is not None:
            try:
                decoded = json.loads(body)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                trace = decoded
                steps = []
                evidence_events = []
                for projected in projected_events:
                    data = projected.get("data")
                    if (
                        projected.get("event_kind") == "iteration.recorded"
                        and isinstance(data, dict)
                        and isinstance(data.get("step"), dict)
                    ):
                        steps.append(data["step"])
                    evidence_events.append(
                        {
                            "sequence": projected.get("sequence"),
                            "kind": projected.get("event_kind"),
                            "timestamp_ns": projected.get("timestamp_ns"),
                            "data": data if isinstance(data, dict) else {},
                        }
                    )
                trace["steps"] = steps
                evidence = trace.get("evidence")
                if isinstance(evidence, dict):
                    evidence["events"] = evidence_events
        envelope = {
            "schema_version": 1,
            "invocation_id": trace_invocation_id or None,
            "status": descriptor.status if descriptor is not None else "in_progress",
            "run_id": (
                trace.get("evidence", {}).get("run_id")
                if isinstance(trace, dict) and isinstance(trace.get("evidence"), dict)
                else None
            ),
            "events": projected_events,
            "trace": trace,
            "error": capture.trace_errors.get(node_id),
        }
        if descriptor is not None or projected_events:
            node.agent_trace_json = json.dumps(envelope, default=str)
    return run


def _replace_queue_contents(subscription: queue.Queue, item: Any) -> None:
    while True:
        try:
            subscription.get_nowait()
        except queue.Empty:
            break
    subscription.put_nowait(item)


_RUN_EVENT_TYPES = {
    "running",
    "node_started",
    "node_succeeded",
    "node_failed",
    "agent_evidence",
    "log",
    "terminal",
}
_NODE_EVENT_TYPES = {"node_started", "node_succeeded", "node_failed"}
_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
_MAX_EVENT_NODES = 10_000
_MAX_EVENT_EDGES = 100_000
_MAX_EVENT_FIELD_LENGTH = 4096
_MAX_EVENT_MESSAGE_LENGTH = 65_536
_MAX_EVENT_AGENT_FIELD_SCHEMA_BYTES = 1024 * 1024
_MAX_EVENT_AGENT_FIELD_SCHEMAS_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_EVENT_TRACEBACK_LENGTH = 262_144
_MAX_EVENT_TIMESTAMP_MAGNITUDE = 10**12


def _validate_agent_detail_depth(value: object) -> None:
    """Reject cyclic or pathologically nested agent-owned detail bodies."""
    active: set[int] = set()

    def walk(item: object, depth: int) -> None:
        if depth > MAX_AGENT_DETAIL_DEPTH:
            raise _CoordinatorProtocolError(
                f"agent detail exceeds maximum depth {MAX_AGENT_DETAIL_DEPTH}"
            )
        if isinstance(item, dict):
            identity = id(item)
            if identity in active:
                raise _CoordinatorProtocolError("agent detail contains a reference cycle")
            active.add(identity)
            try:
                for child in item.values():
                    walk(child, depth + 1)
            finally:
                active.remove(identity)
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise _CoordinatorProtocolError("agent detail contains a reference cycle")
            active.add(identity)
            try:
                for child in item:
                    walk(child, depth + 1)
            finally:
                active.remove(identity)

    walk(value, 0)


def _trace_header_from_payload(trace: dict[str, Any]) -> TraceHeader | None:
    """Validate the stable PredictRLM RunTrace header at the coordinator boundary."""
    if "model" not in trace:
        return None

    status = trace.get("status")
    if type(status) is not str or not status or len(status) > _MAX_EVENT_FIELD_LENGTH:
        raise _CoordinatorProtocolError(
            "agent trace header field 'status' must be a non-empty bounded string"
        )
    model = trace.get("model")
    if type(model) is not str or not model or len(model) > _MAX_EVENT_FIELD_LENGTH:
        raise _CoordinatorProtocolError(
            "agent trace header field 'model' must be a non-empty bounded string"
        )
    sub_model = trace.get("sub_model")
    if sub_model is not None and (
        type(sub_model) is not str or len(sub_model) > _MAX_EVENT_FIELD_LENGTH
    ):
        raise _CoordinatorProtocolError(
            "agent trace header field 'sub_model' must be a bounded string or null"
        )

    iterations = trace.get("iterations")
    if type(iterations) is not int or iterations < 0:
        raise _CoordinatorProtocolError(
            "agent trace header field 'iterations' must be a non-negative integer"
        )
    max_iterations = trace.get("max_iterations")
    if type(max_iterations) is not int or max_iterations < 0:
        raise _CoordinatorProtocolError(
            "agent trace header field 'max_iterations' must be a non-negative integer"
        )
    duration_ms = trace.get("duration_ms")
    if type(duration_ms) is not int or duration_ms < 0:
        raise _CoordinatorProtocolError(
            "agent trace header field 'duration_ms' must be a non-negative integer"
        )

    usage = trace.get("usage")
    if not isinstance(usage, dict):
        raise _CoordinatorProtocolError("agent trace header field 'usage' must be an object")
    telemetry = trace.get("telemetry_ref")
    if telemetry is not None and not isinstance(telemetry, dict):
        raise _CoordinatorProtocolError(
            "agent trace header field 'telemetry_ref' must be an object or null"
        )

    return TraceHeader(
        status=status,
        model=model,
        sub_model=sub_model,
        iterations=iterations,
        max_iterations=max_iterations,
        duration_ms=duration_ms,
        usage_json=json.dumps(usage, separators=(",", ":")),
        telemetry_json=(
            json.dumps(telemetry, separators=(",", ":")) if telemetry is not None else None
        ),
    )


def _validate_preparation_event(event: object) -> str:
    event_type = _event_type(event)
    if event_type == "prepared":
        _require_exact_event_keys(
            event,
            {
                "type",
                "node_ids",
                "graph",
                "node_types",
                "display_names",
                "display_name",
                "agent_field_schemas_json",
                "agent_instruction_lines",
                "standard_step_docstring_lines",
            },
        )
        node_ids = _required_field(event, "node_ids")
        if not isinstance(node_ids, list) or any(
            type(node_id) is not str or len(node_id) > _MAX_EVENT_FIELD_LENGTH
            for node_id in node_ids
        ):
            raise _CoordinatorProtocolError(
                "field 'node_ids' must be a bounded list of bounded strings"
            )
        if len(node_ids) > _MAX_EVENT_NODES:
            raise _CoordinatorProtocolError("field 'node_ids' contains too many nodes")
        if len(node_ids) != len(set(node_ids)):
            raise _CoordinatorProtocolError("field 'node_ids' contains duplicates")
        _graph_mapping(event, "graph")
        node_types = _string_mapping(event, "node_types")
        display_names = _string_mapping(event, "display_names")
        agent_field_schemas_json = _agent_field_schema_mapping(
            event, "agent_field_schemas_json"
        )
        agent_instruction_lines = _string_mapping(event, "agent_instruction_lines")
        standard_step_docstring_lines = _string_mapping(event, "standard_step_docstring_lines")
        for node_id in node_ids:
            if node_id not in node_types:
                raise _CoordinatorProtocolError(
                    f"field 'node_types' is missing node {_bounded_ascii(node_id)}"
                )
            if node_id not in display_names:
                raise _CoordinatorProtocolError(
                    f"field 'display_names' is missing node {_bounded_ascii(node_id)}"
                )
        unknown_agent_nodes = set(agent_field_schemas_json).difference(node_ids)
        if unknown_agent_nodes:
            unknown = min(unknown_agent_nodes)
            raise _CoordinatorProtocolError(
                f"field 'agent_field_schemas_json' references unknown node "
                f"{_bounded_ascii(unknown)}"
            )
        unknown_instruction_nodes = set(agent_instruction_lines).difference(node_ids)
        if unknown_instruction_nodes:
            unknown = min(unknown_instruction_nodes)
            raise _CoordinatorProtocolError(
                f"field 'agent_instruction_lines' references unknown node "
                f"{_bounded_ascii(unknown)}"
            )
        unknown_standard_step_docstring_nodes = set(standard_step_docstring_lines).difference(
            node_ids
        )
        if unknown_standard_step_docstring_nodes:
            unknown = min(unknown_standard_step_docstring_nodes)
            raise _CoordinatorProtocolError(
                f"field 'standard_step_docstring_lines' references unknown node "
                f"{_bounded_ascii(unknown)}"
            )
        display_name = event.get("display_name")
        if display_name is not None and (
            type(display_name) is not str or len(display_name) > _MAX_EVENT_FIELD_LENGTH
        ):
            raise _CoordinatorProtocolError("field 'display_name' must be a bounded string")
        return event_type
    if event_type == "prepare_failed":
        _require_exact_event_keys(event, {"type", "error", "traceback"})
        _string_field(event, "error", maximum_length=_MAX_EVENT_MESSAGE_LENGTH)
        _string_field(event, "traceback", maximum_length=_MAX_EVENT_TRACEBACK_LENGTH)
        return event_type
    if event_type == "log":
        return _validate_run_event(event)
    if event_type in _RUN_EVENT_TYPES:
        raise _CoordinatorProtocolError(
            f"unexpected preparation event type {_bounded_ascii(event_type)}"
        )
    raise _CoordinatorProtocolError(
        f"unknown preparation event type {_bounded_ascii(event_type)}"
    )


def _validate_run_event(event: object, *, validate_result: bool = True) -> str:
    event_type = _event_type(event)
    if event_type not in _RUN_EVENT_TYPES:
        raise _CoordinatorProtocolError(f"unknown run event type {_bounded_ascii(event_type)}")
    if event_type == "running":
        _require_exact_event_keys(event, {"type", "timestamp"})
        _timestamp_field(event, "timestamp")
    elif event_type in _NODE_EVENT_TYPES:
        expected = {"type", "node_id", "timestamp"}
        if event_type == "node_failed":
            expected.add("error")
        _require_exact_event_keys(event, expected)
        _string_field(event, "node_id", maximum_length=_MAX_EVENT_FIELD_LENGTH)
        _timestamp_field(event, "timestamp")
        if event_type == "node_failed":
            _string_field(event, "error", maximum_length=_MAX_EVENT_MESSAGE_LENGTH)
    elif event_type == "agent_evidence":
        _require_exact_event_keys(event, {"type", "node_id", "event"})
        _string_field(event, "node_id", maximum_length=_MAX_EVENT_FIELD_LENGTH)
        agent_event = _required_field(event, "event")
        if type(agent_event) is not dict:
            raise _CoordinatorProtocolError("field 'event' must be a dict")
    elif event_type == "log":
        _require_exact_event_keys(
            event,
            {"type", "timestamp", "level", "node_id", "message"},
        )
        timestamp = _timestamp_field(event, "timestamp")
        try:
            datetime.fromtimestamp(timestamp)
        except (OSError, OverflowError, ValueError) as exc:
            raise _CoordinatorProtocolError(
                "field 'timestamp' is outside the supported datetime range"
            ) from exc
        level = _required_field(event, "level")
        if type(level) is not int or not -(2**31) <= level < 2**31:
            raise _CoordinatorProtocolError("field 'level' must be a bounded integer")
        _string_field(event, "node_id", maximum_length=_MAX_EVENT_FIELD_LENGTH)
        _string_field(event, "message", maximum_length=_MAX_EVENT_MESSAGE_LENGTH)
    else:
        status = _string_field(
            event,
            "status",
            maximum_length=_MAX_EVENT_FIELD_LENGTH,
        )
        if status not in _TERMINAL_STATUSES:
            raise _CoordinatorProtocolError(f"invalid terminal status {_bounded_ascii(status)}")
        if status == "success":
            _require_exact_event_keys(
                event,
                {"type", "status", "result_manifest_sha256"},
            )
            if validate_result:
                _result_manifest_digest_from_event(event)
        elif status == "failed":
            _require_exact_event_keys(event, {"type", "status", "error"})
            _string_field(event, "error", maximum_length=_MAX_EVENT_MESSAGE_LENGTH)
        else:
            _require_exact_event_keys(event, {"type", "status"})
    return event_type


def _is_provisional_success_event(event: object) -> bool:
    return (
        type(event) is dict
        and event.get("type") == "terminal"
        and event.get("status") == "success"
    )


def _event_after_provisional_success(event_queue: Any) -> object | None:
    """Return the first post-success event after a quiesced writer, if any."""
    for _ in range(3):
        try:
            return event_queue.get(timeout=0.02)
        except queue.Empty:
            continue
    return None


def _close_event_queue(event_queue: Any) -> None:
    event_queue.close()
    join_thread = getattr(event_queue, "join_thread", None)
    if callable(join_thread):
        join_thread()


def _result_manifest_digest_from_event(event: dict[str, Any]) -> str:
    digest = _string_field(event, "result_manifest_sha256")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise _CoordinatorProtocolError(
            "field 'result_manifest_sha256' must be a lowercase hexadecimal SHA-256"
        )
    return digest


def _event_type(event: object) -> str:
    if type(event) is not dict:
        raise _CoordinatorProtocolError(f"event must be a dict, got {type(event).__name__}")
    event_type = _required_field(event, "type")
    if type(event_type) is not str:
        raise _CoordinatorProtocolError("field 'type' must be a string")
    if len(event_type) > _MAX_EVENT_FIELD_LENGTH:
        raise _CoordinatorProtocolError("field 'type' exceeds the maximum length")
    return event_type


def _required_field(event: dict[str, Any], field: str) -> Any:
    if field not in event:
        raise _CoordinatorProtocolError(f"missing required field {field!r}")
    return event[field]


def _string_field(
    event: dict[str, Any],
    field: str,
    *,
    maximum_length: int = _MAX_EVENT_FIELD_LENGTH,
) -> str:
    value = _required_field(event, field)
    if type(value) is not str:
        raise _CoordinatorProtocolError(f"field {field!r} must be a string")
    if len(value) > maximum_length:
        raise _CoordinatorProtocolError(f"field {field!r} exceeds the maximum length")
    return value


def _timestamp_field(event: dict[str, Any], field: str) -> float | int:
    value = _required_field(event, field)
    if type(value) is int and abs(value) <= _MAX_EVENT_TIMESTAMP_MAGNITUDE:
        return value
    if (
        type(value) is float
        and math.isfinite(value)
        and abs(value) <= _MAX_EVENT_TIMESTAMP_MAGNITUDE
    ):
        return value
    raise _CoordinatorProtocolError(f"field {field!r} must be a bounded finite number")


def _string_mapping(
    event: dict[str, Any],
    field: str,
    *,
    maximum_value_length: int | None = _MAX_EVENT_FIELD_LENGTH,
) -> Mapping[str, str]:
    value = _required_field(event, field)
    if (
        type(value) is not dict
        or len(value) > _MAX_EVENT_NODES
        or any(
            type(key) is not str
            or type(item) is not str
            or len(key) > _MAX_EVENT_FIELD_LENGTH
            or (maximum_value_length is not None and len(item) > maximum_value_length)
            for key, item in value.items()
        )
    ):
        raise _CoordinatorProtocolError(f"field {field!r} must map bounded strings to strings")
    return value


def _agent_field_schema_mapping(event: dict[str, Any], field: str) -> Mapping[str, str]:
    value = _string_mapping(event, field, maximum_value_length=None)
    total_bytes = 0
    for schema_json in value.values():
        try:
            encoded_size = len(schema_json.encode("utf-8"))
            schema = json.loads(schema_json)
        except (UnicodeEncodeError, json.JSONDecodeError) as exc:
            raise _CoordinatorProtocolError(
                f"field {field!r} values must be UTF-8 JSON objects"
            ) from exc
        if encoded_size > _MAX_EVENT_AGENT_FIELD_SCHEMA_BYTES:
            raise _CoordinatorProtocolError(
                f"field {field!r} values must not exceed "
                f"{_MAX_EVENT_AGENT_FIELD_SCHEMA_BYTES} UTF-8 bytes"
            )
        total_bytes += encoded_size
        if type(schema) is not dict or set(schema) != {"inputs", "outputs"}:
            raise _CoordinatorProtocolError(
                f"field {field!r} values must contain only input and output schemas"
            )
        for schemas in schema.values():
            if type(schemas) is not list or len(schemas) > _MAX_EVENT_NODES:
                raise _CoordinatorProtocolError(
                    f"field {field!r} inputs and outputs must be bounded lists"
                )
            if any(
                type(item) is not dict
                or set(item) != {"name", "type", "description"}
                or type(item["name"]) is not str
                or not item["name"]
                or len(item["name"]) > _MAX_EVENT_FIELD_LENGTH
                or type(item["type"]) is not str
                or len(item["type"]) > _MAX_EVENT_FIELD_LENGTH
                or type(item["description"]) is not str
                or len(item["description"]) > _MAX_EVENT_MESSAGE_LENGTH
                for item in schemas
            ):
                raise _CoordinatorProtocolError(
                    f"field {field!r} contains an invalid invocation field schema"
                )
    if total_bytes > _MAX_EVENT_AGENT_FIELD_SCHEMAS_TOTAL_BYTES:
        raise _CoordinatorProtocolError(
            f"field {field!r} must not exceed "
            f"{_MAX_EVENT_AGENT_FIELD_SCHEMAS_TOTAL_BYTES} total UTF-8 bytes"
        )
    return value


def _graph_mapping(event: dict[str, Any], field: str) -> Mapping[str, list[str]]:
    value = _required_field(event, field)
    if (
        type(value) is not dict
        or len(value) > _MAX_EVENT_NODES
        or any(
            type(key) is not str
            or len(key) > _MAX_EVENT_FIELD_LENGTH
            or not isinstance(children, list)
            or len(children) > _MAX_EVENT_NODES
            or any(type(child) is not str for child in children)
            for key, children in value.items()
        )
    ):
        raise _CoordinatorProtocolError(f"field {field!r} must map strings to lists of strings")
    if any(
        len(child) > _MAX_EVENT_FIELD_LENGTH
        for children in value.values()
        for child in children
    ):
        raise _CoordinatorProtocolError(f"field {field!r} contains an oversized node ID")
    if sum(len(children) for children in value.values()) > _MAX_EVENT_EDGES:
        raise _CoordinatorProtocolError(f"field {field!r} contains too many edges")
    return value


def _require_exact_event_keys(
    event: dict[str, Any],
    expected: set[str],
) -> None:
    actual = set(event)
    if actual != expected:
        extra = sorted(actual - expected)
        missing = sorted(expected - actual)
        if extra:
            raise _CoordinatorProtocolError(
                f"unexpected event field {_bounded_ascii(extra[0])}"
            )
        raise _CoordinatorProtocolError(f"missing required field {_bounded_ascii(missing[0])}")


def _bounded_ascii(value: object, limit: int = 80) -> str:
    if type(value) is str:
        rendered = str.__repr__(value)
    elif type(value) is int:
        # Avoid both unbounded work and Python's configurable integer-to-string
        # digit limit when describing hostile protocol values.
        rendered = "<int>" if value.bit_length() > 256 else str(value)
    elif type(value) is float:
        rendered = repr(value)
    elif type(value) is bool:
        rendered = "True" if value else "False"
    elif value is None:
        rendered = "None"
    else:
        rendered = f"<{type(value).__name__}>"
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _protocol_fault_message(
    event: object, exc: _CoordinatorProtocolError, limit: int = 400
) -> str:
    if type(event) is dict:
        raw_type = dict.get(event, "type", "<missing>")
        event_label = _bounded_ascii(raw_type)
    else:
        event_label = type(event).__name__
    message = f"Malformed coordinator event {event_label}: {exc}"
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _resolve_executor_config(
    *,
    executor_backend: ExecutorBackend | _ExecutorBackendOmitted,
    ray_runtime_env: Mapping[str, Any] | None,
    ray_init_kwargs: Mapping[str, Any] | None,
    executor_factory: DeprecatedExecutorFactory | None,
) -> dict[str, Any]:
    backend_was_provided = not isinstance(executor_backend, _ExecutorBackendOmitted)
    backend: ExecutorBackend = "local" if not backend_was_provided else executor_backend
    if executor_factory is not None:
        warnings.warn(
            "executor_factory is deprecated; use executor_backend and the "
            "ray_runtime_env/ray_init_kwargs configuration instead",
            DeprecationWarning,
            stacklevel=3,
        )
        if backend_was_provided or ray_runtime_env is not None or ray_init_kwargs is not None:
            raise TypeError(
                "executor_factory cannot be combined with executor_backend or "
                "Ray configuration"
            )
        return _deprecated_executor_config(executor_factory)

    if backend not in {"local", "ray"}:
        raise ValueError(f"Unsupported executor_backend {backend!r}; expected 'local' or 'ray'")
    if backend == "local":
        if ray_runtime_env is not None or ray_init_kwargs is not None:
            raise ValueError(
                "ray_runtime_env and ray_init_kwargs require executor_backend='ray'"
            )
        return {"backend": "local"}
    return {
        "backend": "ray",
        "runtime_env": dict(ray_runtime_env or {}),
        "ray_init_kwargs": dict(ray_init_kwargs or {}),
    }


def _deprecated_executor_config(
    factory: DeprecatedExecutorFactory,
) -> dict[str, Any]:
    if factory is LocalExecutor:
        return {"backend": "local"}
    if factory is RayExecutor:
        return {"backend": "ray", "runtime_env": {}, "ray_init_kwargs": {}}
    if isinstance(factory, partial) and factory.func is RayExecutor:
        if factory.args:
            raise TypeError("RayExecutor partial must use keyword arguments only")
        unsupported = set(factory.keywords or {}) - {"runtime_env", "ray_init_kwargs"}
        if unsupported:
            raise TypeError(
                "Unsupported RayExecutor partial arguments: " + ", ".join(sorted(unsupported))
            )
        return {
            "backend": "ray",
            "runtime_env": dict((factory.keywords or {}).get("runtime_env") or {}),
            "ray_init_kwargs": dict((factory.keywords or {}).get("ray_init_kwargs") or {}),
        }
    raise TypeError(
        "Unsupported executor_factory. Per-run spawn requires serializable backend "
        "configuration; use executor_backend='local' or executor_backend='ray' with "
        "ray_runtime_env/ray_init_kwargs. The deprecated compatibility parameter only "
        "accepts exact LocalExecutor, exact RayExecutor, or a keyword-only "
        "functools.partial(RayExecutor, ...)."
    )


def _teardown_process_group(
    process: multiprocessing.Process,
    windows_job: WindowsJob | None,
    *,
    wait_before_term: float = 0.0,
    term_grace: float = 1.0,
    kill_grace: float = 1.0,
) -> _ProcessGroupTeardown:
    """Boundedly stop a coordinator session and all of its descendants."""
    if process.pid is None:
        close_job(windows_job)
        return _ProcessGroupTeardown(True, process.exitcode)
    if wait_before_term:
        process.join(timeout=wait_before_term)
    natural_exitcode = process.exitcode if not process.is_alive() else None
    if os.name != "nt":
        group_signalled = _signal_coordinator_group(process.pid, signal.SIGTERM)
        if not group_signalled and process.is_alive():
            process.terminate()
    elif process.is_alive():
        process.terminate()
    quiesced = _wait_for_coordinator_group_quiescence(process, term_grace)
    if not quiesced:
        if os.name != "nt":
            group_signalled = _signal_coordinator_group(process.pid, signal.SIGKILL)
            if not group_signalled and process.is_alive():
                process.kill()
        elif hasattr(process, "kill"):
            process.kill()
        quiesced = _wait_for_coordinator_group_quiescence(process, kill_grace)
    close_job(windows_job)
    return _ProcessGroupTeardown(quiesced, natural_exitcode)


def _wait_for_coordinator_group_quiescence(
    process: multiprocessing.Process,
    timeout: float,
) -> bool:
    """Wait boundedly for both the coordinator and its descendants to stop."""
    process_group = process.pid
    if process_group is None:
        return not process.is_alive()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        process_alive = process.is_alive()
        group_alive = os.name != "nt" and _coordinator_group_has_live_members(process_group)
        if not process_alive and not group_alive:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        poll_interval = min(_PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining)
        if process_alive:
            process.join(timeout=poll_interval)
        else:
            time.sleep(poll_interval)


def _signal_coordinator_group(process_group: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _coordinator_group_exists(process_group: int) -> bool | None:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def _coordinator_group_has_live_members(process_group: int) -> bool:
    """Treat dead or irreversibly exiting groups as quiesced."""
    group_exists = _coordinator_group_exists(process_group)
    if group_exists is False:
        return False
    if group_exists is None:
        return True
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,stat="],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            pgid = int(fields[0])
        except ValueError:
            continue
        state = fields[1]
        dead = state.startswith(("Z", "X"))
        irreversibly_exiting = state.startswith("?") and "E" in state[1:]
        if pgid == process_group and not (dead or irreversibly_exiting):
            return True
    return False
