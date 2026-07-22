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
from .models import (
    AgentEvent,
    AgentEventPage,
    FinalizedTrace,
    LogEntry,
    LogLevel,
    LogPage,
    NodeSnapshot,
    NodeState,
    NodeStatus,
    RunSnapshot,
    RunState,
    RunStatus,
    RunSummary,
    RunSummaryPage,
    SequencedLogEntry,
    TraceDescriptor,
    WorkflowInfo,
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
from .source import is_source_path_included, resolve_live_source, resolve_watch_roots
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
STRUCTURAL_BASELINE_CAPACITY = 8
_MISSING_WORKFLOW_ID = "\0"
DeprecatedExecutorFactory: TypeAlias = (
    type[LocalExecutor] | type[RayExecutor] | partial[RayExecutor]
)


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
    subscribers: tuple[tuple[queue.Queue, RunState], ...]
    ready: threading.Event
    delivered: threading.Event


@dataclass(frozen=True)
class _AgentEvidenceMutation:
    entry: LogEntry
    finalized_trace: bytes | None = None


@dataclass(frozen=True)
class _StructuralBaseline:
    as_of_sequence: int
    summaries: tuple[RunSummary, ...]
    snapshots: Mapping[str, RunSnapshot]

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
        discovery_timeout: float = 15.0,
        cancel_grace: float = 5.0,
        stream_history_capacity: int = STREAM_HISTORY_CAPACITY,
        result_storage_directory: str | os.PathLike[str] | None = None,
        result_retention_seconds: float | None = DEFAULT_RESULT_RETENTION_SECONDS,
        webhook_port: int = DEFAULT_WEBHOOK_PORT,
        structural_baseline_capacity: int = STRUCTURAL_BASELINE_CAPACITY,
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
        self._mp = multiprocessing.get_context("spawn")
        self._runs: dict[str, RunState] = {}
        self._stored_results: dict[str, StoredWorkflowResult] = {}
        self._result_leases: dict[str, int] = {}
        self._run_created_sequences: dict[str, int] = {}
        self._run_revisions: dict[str, int] = {}
        self._node_revisions: dict[tuple[str, str], int] = {}
        self._logs: dict[str, list[SequencedLogEntry]] = {}
        self._agent_events: dict[tuple[str, str], list[AgentEvent]] = {}
        self._trace_descriptors: dict[tuple[str, str], TraceDescriptor] = {}
        self._trace_bodies: dict[tuple[str, str], dict[int, bytes]] = {}
        self._active_runs: dict[str, _RunHandle] = {}
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._subscribers: list[queue.Queue] = []
        self._operator_instance_id = uuid4().hex
        self._sequence = 0
        self._stream_history: deque[tuple[int, RunState]] = deque(
            maxlen=stream_history_capacity
        )
        self._structural_baseline_capacity = structural_baseline_capacity
        self._structural_baselines: OrderedDict[int, _StructuralBaseline] = (
            OrderedDict()
        )
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

    def list_workflows(self) -> list[WorkflowInfo]:
        workflows = self._registry.list_workflows()
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
        return workflows

    def list_diagnostics(self):
        return self._registry.list_diagnostics()

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        with self._lock:
            runs = deepcopy(list(self._runs.values()))
        return self._matching_runs(runs, workflow_selector)

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run is not None else None

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
                baseline = self._retained_structural_baseline_locked(
                    token["as_of_sequence"]
                )
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

            summaries = [
                summary
                for summary in baseline.summaries
                if not resolved_workflow_id
                or (summary.workflow_id or summary.flow_name)
                == resolved_workflow_id
            ]
            summaries.sort(key=lambda item: (-item.created_sequence, item.run_id))
            if cursor_sequence is not None:
                summaries = [
                    item
                    for item in summaries
                    if item.created_sequence < cursor_sequence
                    or (
                        item.created_sequence == cursor_sequence
                        and item.run_id > cursor_run_id
                    )
                ]

            selected = summaries[:size]
            next_page_token = ""
            if len(summaries) > size:
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

    def list_logs(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        page_size: int = 0,
    ) -> LogPage:
        """Return append-only logs after an exclusive per-run cursor."""
        size = _bounded_page_size(page_size)
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            candidates = [
                item for item in self._logs.get(run_id, ()) if item.sequence > after_sequence
            ]
            selected = candidates[:size]
            next_sequence = selected[-1].sequence if selected else after_sequence
            return LogPage(
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=self._sequence,
                logs=tuple(deepcopy(selected)),
                next_sequence=next_sequence,
                has_more=len(candidates) > size,
            )

    def list_agent_events(
        self,
        run_id: str,
        node_id: str,
        *,
        after_event_sequence: int = 0,
        page_size: int = 0,
    ) -> AgentEventPage:
        """Return deduplicated projected events after an exclusive source cursor."""
        size = _bounded_page_size(page_size)
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if node_id not in run.nodes:
                raise KeyError(node_id)
            candidates = [
                item
                for item in self._agent_events.get((run_id, node_id), ())
                if item.event_sequence > after_event_sequence
            ]
            selected = candidates[:size]
            next_sequence = (
                selected[-1].event_sequence if selected else after_event_sequence
            )
            return AgentEventPage(
                operator_instance_id=self._operator_instance_id,
                as_of_sequence=self._sequence,
                run_id=run_id,
                node_id=node_id,
                events=tuple(selected),
                next_event_sequence=next_sequence,
                has_more=len(candidates) > size,
            )

    def read_trace(
        self,
        run_id: str,
        node_id: str,
        *,
        revision: int = 0,
    ) -> FinalizedTrace:
        """Copy one immutable finalized trace body out of operator-owned memory."""
        with self._lock:
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
        for run in self._runs.values():
            summary = self._run_summary_locked(run)
            summaries.append(summary)
            snapshots[run.run_id] = self._run_snapshot_locked(
                run,
                summary=summary,
                as_of_sequence=as_of_sequence,
            )
        baseline = _StructuralBaseline(
            as_of_sequence=as_of_sequence,
            summaries=tuple(summaries),
            snapshots=MappingProxyType(snapshots),
        )
        self._structural_baselines[as_of_sequence] = baseline
        while len(self._structural_baselines) > self._structural_baseline_capacity:
            self._structural_baselines.popitem(last=False)
        return baseline

    def _retained_structural_baseline_locked(
        self, as_of_sequence: int
    ) -> _StructuralBaseline:
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
                trace=self._trace_descriptors.get((run.run_id, node.node_id)),
                revision=self._node_revisions.get(
                    (run.run_id, node.node_id),
                    self._run_created_sequences.get(run.run_id, 0),
                ),
            )
            for node in run.nodes.values()
        )
        logs = self._logs.get(run.run_id, ())
        return RunSnapshot(
            operator_instance_id=self._operator_instance_id,
            as_of_sequence=as_of_sequence,
            summary=summary,
            nodes=nodes,
            latest_log_sequence=logs[-1].sequence if logs else 0,
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

    def _matching_runs(
        self, runs: list[RunState], workflow_selector: str
    ) -> list[RunState]:
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
            if status in {RunStatus.PENDING, RunStatus.RUNNING}:
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
        """Synchronously prepare a live-source run before publishing its ID."""
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
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while creating run")
                process.start()
                if process.pid is None:
                    raise RuntimeError("Run coordinator did not expose a process ID")
                assign_process(windows_job, process.pid)
                assignment_event.set()
            prepared, buffered_events = self._await_prepared(handle)
            run = self._run_from_prepared(
                run_id,
                descriptor.workflow_id,
                descriptor.display_name,
                triggered_by,
                prepared,
            )
            drain = threading.Thread(
                target=self._drain_run_events,
                args=(run_id, handle, buffered_events),
                name=f"avalanche-drain-{run_id}",
                daemon=True,
            )
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while preparing run")
                handle.drain_thread = drain
                drain.start()
                self._runs[run_id] = run
                notifications = self._publish_run_locked(run)
            self._wait_for_notifications(notifications)
            handle.publication_event.set()
            start_event.set()
            return run_id
        except BaseException:
            cancel_event.set()
            handle.publication_event.set()
            start_event.set()
            _teardown_process_group(process, windows_job)
            with self._lock:
                self._runs.pop(run_id, None)
                self._stored_results.pop(run_id, None)
                self._active_runs.pop(run_id, None)
            self._result_store.discard(result_bundle)
            _close_event_queue(event_queue)
            raise

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
            if run is None or run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
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

    def subscribe(self, since_sequence: int = 0) -> queue.Queue:
        subscription: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(subscription)
            current_sequence = self._sequence
            oldest_sequence = (
                self._stream_history[0][0] if self._stream_history else current_sequence + 1
            )
            cursor_is_replayable = (
                since_sequence <= current_sequence and since_sequence >= oldest_sequence - 1
            )
            if cursor_is_replayable:
                for sequence, run in self._stream_history:
                    if sequence > since_sequence:
                        subscription.put_nowait((sequence, deepcopy(run)))
            else:
                for run_id in sorted(self._runs):
                    self._sequence += 1
                    snapshot = deepcopy(self._runs[run_id])
                    self._stream_history.append((self._sequence, snapshot))
                    subscription.put_nowait((self._sequence, deepcopy(snapshot)))
        return subscription

    def unsubscribe(self, subscription: queue.Queue) -> None:
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscription]

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
        for changes in watch(
            *watch_dirs,
            stop_event=self._watcher_stop,
            watch_filter=lambda _, path: is_source_path_included(path, source_roots),
            rust_timeout=50,
            yield_on_timeout=True,
        ):
            self._watcher_ready.set()
            if not changes:
                continue
            changed_files = [path for _, path in changes]
            logging.getLogger(__name__).info(
                "Workflow files changed: %s, re-scanning...", changed_files
            )
            self._refresh_workflows()

    def _refresh_workflows(self) -> None:
        # Publishing descriptors and replacing schedules are one logical update.
        # Otherwise an old cron can resolve newly-published same-ID source in the
        # small window between these two operations.
        with self._scheduler.reconciliation_boundary():
            try:
                view = self._registry.rescan(validate=routes_for)
            except ValueError as exc:
                logging.getLogger(__name__).warning("Webhook catalog refresh rejected: %s", exc)
                return
            self._scheduler.reconcile(view.by_id.values())
            self._reconcile_webhooks(view.by_id.values())

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
        prepared: dict[str, Any],
    ) -> RunState:
        display_name = prepared.get("display_name") or catalog_display_name
        run = RunState(
            run_id=run_id,
            flow_name=display_name,
            workflow_id=workflow_id,
            workflow_display_name=display_name,
            status=RunStatus.PENDING,
            triggered_by=triggered_by,
        )
        for node_id in prepared["node_ids"]:
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
                trace_node_ids: tuple[str, ...] = ()
                finalized_traces: dict[str, bytes] = {}
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
                        changed_node_ids = (node.node_id,)
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
                    log_entry = LogEntry(
                        timestamp=datetime.fromtimestamp(event["timestamp"]),
                        level=_LEVEL_MAP.get(event["level"], LogLevel.INFO),
                        node_id=event["node_id"],
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
                    summary_changed = True
                    mutated = True

                if not mutated:
                    return terminal
                notifications = self._publish_run_locked(
                    run,
                    summary_changed=summary_changed,
                    changed_node_ids=changed_node_ids,
                    trace_node_ids=trace_node_ids,
                    finalized_traces=finalized_traces,
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
                    trace_node_ids=(node_id,),
                    finalized_traces=finalized_traces,
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
        node = run.nodes.get(node_id)
        if node is None or not isinstance(event, dict):
            return None
        envelope = (
            json.loads(node.agent_trace_json)
            if node.agent_trace_json is not None
            else {
                "schema_version": 1,
                "status": "in_progress",
                "run_id": None,
                "events": [],
                "trace": None,
                "error": None,
            }
        )
        if not isinstance(envelope, dict):
            return None
        events = envelope.get("events")
        if not isinstance(events, list):
            return None

        key = (run.run_id, node_id)
        projected_events = self._agent_events.setdefault(key, [])
        previous_descriptor = self._trace_descriptors.get(
            key, TraceDescriptor(status="in_progress")
        )
        finalized_trace: bytes | None = None
        projected_agent_event: AgentEvent | None = None
        kind = event.get("kind")
        level = LogLevel.INFO
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
            if projected_events and sequence <= projected_events[-1].event_sequence:
                return None
            projected = {
                "sequence": sequence,
                "event_kind": event_kind,
                "timestamp_ns": timestamp_ns,
                "data": data,
            }
            projected_agent_event = AgentEvent(
                event_sequence=sequence,
                event_json=json.dumps(
                    projected,
                    default=str,
                    separators=(",", ":"),
                ),
            )
            events.append(projected)
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
            if data.get("error") or event_kind in {"run.failed", "run.cancelled"}:
                level = LogLevel.ERROR
                envelope["status"] = "error"
                envelope["error"] = str(data.get("error") or event_kind)
            descriptor = TraceDescriptor(
                status=str(envelope.get("status") or "in_progress"),
                revision=previous_descriptor.revision,
                available=previous_descriptor.available,
                complete=previous_descriptor.complete,
                event_count=len(projected_events) + 1,
                size_bytes=previous_descriptor.size_bytes,
            )
        elif kind == "trace_finished":
            trace = event.get("trace")
            if not isinstance(trace, dict):
                return None
            finalized_trace = json.dumps(
                trace,
                default=str,
                separators=(",", ":"),
            ).encode()
            versions = self._trace_bodies.get(key, {})
            if (
                previous_descriptor.available
                and versions.get(previous_descriptor.revision) == finalized_trace
            ):
                return None
            envelope["trace"] = trace
            envelope["status"] = str(trace.get("status") or "unavailable")
            evidence = trace.get("evidence")
            if isinstance(evidence, dict):
                envelope["run_id"] = evidence.get("run_id")
            descriptor = TraceDescriptor(
                status=envelope["status"],
                revision=previous_descriptor.revision,
                available=True,
                complete=bool(isinstance(evidence, dict) and evidence.get("complete")),
                event_count=len(projected_events),
                size_bytes=len(finalized_trace),
            )
            message = f"Agent trace {envelope['status']}"
            if envelope["status"] == "error":
                level = LogLevel.ERROR
        elif kind == "trace_unavailable":
            error = event.get("error")
            envelope["status"] = "unavailable"
            envelope["error"] = str(error or "Agent trace unavailable")
            descriptor = TraceDescriptor(
                status="unavailable",
                revision=previous_descriptor.revision,
                available=False,
                complete=False,
                event_count=len(projected_events),
            )
            message = f"Agent trace unavailable: {envelope['error']}"
            level = LogLevel.WARN
        else:
            return None

        envelope_json = json.dumps(envelope, default=str)
        entry = LogEntry(
            timestamp=datetime.now(),
            level=level,
            node_id=node_id,
            message=message,
        )
        if projected_agent_event is not None:
            projected_events.append(projected_agent_event)
        self._trace_descriptors[key] = descriptor
        # Compatibility mirror for ListRuns/GetRun/StreamUpdates until
        # the aggregate client cutover. New structural reads never use it.
        node.agent_trace_json = envelope_json
        self._append_log_locked(run, entry)
        return _AgentEvidenceMutation(entry=entry, finalized_trace=finalized_trace)

    def _append_log_locked(self, run: RunState, entry: LogEntry) -> None:
        logs = self._logs.setdefault(run.run_id, [])
        logs.append(SequencedLogEntry(sequence=len(logs) + 1, entry=deepcopy(entry)))
        # Compatibility mirror for the pre-cutover transport.
        run.logs.append(entry)


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
        trace_node_ids: tuple[str, ...] = (),
        finalized_traces: Mapping[str, bytes] | None = None,
        log_entry: LogEntry | None = None,
    ) -> _RunNotifications:
        """Advance state and detail watermarks while the caller holds ``_lock``."""
        if self._notification_stop_enqueued:
            raise RuntimeError("Operator notification dispatcher is closed")
        self._sequence += 1
        sequence = self._sequence
        is_new = run.run_id not in self._run_created_sequences
        created_sequence = self._run_created_sequences.setdefault(run.run_id, sequence)
        if summary_changed or is_new:
            self._run_revisions[run.run_id] = sequence
        if is_new:
            for node_id in run.nodes:
                self._node_revisions[(run.run_id, node_id)] = created_sequence
        for node_id in changed_node_ids:
            self._node_revisions[(run.run_id, node_id)] = sequence

        finalized_traces = finalized_traces or {}
        for node_id in trace_node_ids:
            key = (run.run_id, node_id)
            descriptor = self._trace_descriptors[key]
            versions = self._trace_bodies.setdefault(key, {})
            finalized_trace = finalized_traces.get(node_id)
            if finalized_trace is not None:
                versions[sequence] = finalized_trace
            elif descriptor.available and descriptor.revision in versions:
                versions[sequence] = versions[descriptor.revision]
            self._trace_descriptors[key] = replace(descriptor, revision=sequence)

        snapshot = deepcopy(run)
        self._stream_history.append((sequence, snapshot))
        notifications = _RunNotifications(
            sequence=sequence,
            run_callbacks=tuple(
                (callback, deepcopy(run)) for callback in self._run_callbacks
            ),
            log_callbacks=(
                tuple(
                    (callback, deepcopy(log_entry)) for callback in self._log_callbacks
                )
                if log_entry is not None
                else ()
            ),
            subscribers=tuple(
                (subscription, deepcopy(run)) for subscription in self._subscribers
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

    @staticmethod
    def _deliver_notifications(notifications: _RunNotifications) -> None:
        for callback, entry in notifications.log_callbacks:
            try:
                callback(entry)
            except Exception:
                pass
        for callback, snapshot in notifications.run_callbacks:
            try:
                callback(snapshot)
            except Exception:
                pass
        for subscription, snapshot in notifications.subscribers:
            try:
                subscription.put_nowait((notifications.sequence, snapshot))
            except queue.Full:
                pass



def _bounded_page_size(page_size: int) -> int:
    if page_size < 0:
        raise ValueError("Page size must be non-negative")
    return min(page_size or DETAIL_PAGE_SIZE, MAX_DETAIL_PAGE_SIZE)


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
        type(value.get(field)) is not expected
        for field, expected in expected_types.items()
    ):
        raise ValueError("Invalid page token")
    return value


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
_MAX_EVENT_TRACEBACK_LENGTH = 262_144
_MAX_EVENT_TIMESTAMP_MAGNITUDE = 10**12


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
        for node_id in node_ids:
            if node_id not in node_types:
                raise _CoordinatorProtocolError(
                    f"field 'node_types' is missing node {_bounded_ascii(node_id)}"
                )
            if node_id not in display_names:
                raise _CoordinatorProtocolError(
                    f"field 'display_names' is missing node {_bounded_ascii(node_id)}"
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


def _string_mapping(event: dict[str, Any], field: str) -> Mapping[str, str]:
    value = _required_field(event, field)
    if (
        type(value) is not dict
        or len(value) > _MAX_EVENT_NODES
        or any(
            type(key) is not str
            or type(item) is not str
            or len(key) > _MAX_EVENT_FIELD_LENGTH
            or len(item) > _MAX_EVENT_FIELD_LENGTH
            for key, item in value.items()
        )
    ):
        raise _CoordinatorProtocolError(f"field {field!r} must map strings to strings")
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
