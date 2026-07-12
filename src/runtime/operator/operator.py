"""Operator — isolated run coordination and parent-owned state."""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import signal
import threading
import time
import warnings
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Callable, Literal, TypeAlias
from uuid import uuid4

from ..executor import LocalExecutor, RayExecutor
from .models import LogEntry, LogLevel, NodeState, NodeStatus, RunState, RunStatus, WorkflowInfo
from .registry import AmbiguousWorkflow, WorkflowRegistry
from .run_worker import run_worker
from .source import is_source_path_included, resolve_live_source, resolve_watch_roots
from .windows_job import WindowsJob, assign_process, close_job, create_kill_on_close_job

_LEVEL_MAP = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARN,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.ERROR,
}

ExecutorBackend: TypeAlias = Literal["local", "ray"]
DeprecatedExecutorFactory: TypeAlias = (
    type[LocalExecutor] | type[RayExecutor] | partial[RayExecutor]
)


class _ExecutorBackendOmitted:
    pass


_EXECUTOR_BACKEND_OMITTED = _ExecutorBackendOmitted()


@dataclass
class _RunHandle:
    process: multiprocessing.Process
    event_queue: Any
    cancel_event: Any
    start_event: Any
    assignment_event: Any
    windows_job: WindowsJob | None
    drain_thread: threading.Thread | None = None


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
        self._registry = WorkflowRegistry(discovery_timeout=discovery_timeout)
        self._workflow_paths = workflow_paths or []
        if self._workflow_paths:
            self._registry.scan(self._workflow_paths)

        self._prepare_timeout = prepare_timeout
        if cancel_grace < 0:
            raise ValueError("Cancellation grace must be non-negative")
        self._cancel_grace = cancel_grace
        self._mp = multiprocessing.get_context("spawn")
        self._runs: dict[str, RunState] = {}
        self._active_runs: dict[str, _RunHandle] = {}
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._subscribers: list[queue.Queue] = []
        self._sequence = 0
        self._lock = threading.RLock()
        self._watcher_stop = threading.Event()
        self._watcher_ready = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._closed = False

        from .scheduler import Scheduler

        self._scheduler = Scheduler(self)
        self._scheduler.reconcile(self._registry.descriptors())
        if watch and self._workflow_paths:
            self._start_watcher()
        if schedule and self._workflow_paths:
            self._scheduler.start()

    def list_workflows(self) -> list[WorkflowInfo]:
        workflows = self._registry.list_workflows()
        for info in workflows:
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
        exact = [run for run in runs if run.workflow_id == workflow_selector]
        if exact:
            return exact
        try:
            workflow_id = self._registry.resolve(workflow_selector).workflow_id
        except AmbiguousWorkflow:
            raise
        except KeyError:
            matching_ids = sorted({
                run.workflow_id or run.flow_name
                for run in runs
                if run.flow_name == workflow_selector
                or run.workflow_display_name == workflow_selector
            })
            if len(matching_ids) > 1:
                raise AmbiguousWorkflow(workflow_selector, tuple(matching_ids)) from None
            if not matching_ids:
                return []
            workflow_id = matching_ids[0]
        return [run for run in runs if (run.workflow_id or run.flow_name) == workflow_id]

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run is not None else None

    def start_run(
        self,
        flow_name: str,
        triggered_by: str = "manual",
        *,
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
            run_id = f"run_{str(uuid4())[:8]}"
            event_queue = self._mp.Queue()
            cancel_event = self._mp.Event()
            start_event = self._mp.Event()
            assignment_event = self._mp.Event()
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
                ),
                name=f"avalanche-run-{run_id}",
                daemon=False,
            )
            windows_job = create_kill_on_close_job()
            handle = _RunHandle(
                process,
                event_queue,
                cancel_event,
                start_event,
                assignment_event,
                windows_job,
            )
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while creating run")
                self._active_runs[run_id] = handle
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
            with self._lock:
                if self._closed:
                    raise RuntimeError("Operator closed while preparing run")
                self._runs[run_id] = run
                drain = threading.Thread(
                    target=self._drain_run_events,
                    args=(run_id, handle, buffered_events),
                    name=f"avalanche-drain-{run_id}",
                    daemon=True,
                )
                handle.drain_thread = drain
                drain.start()
            self._notify_run(run)
            start_event.set()
            return run_id
        except BaseException:
            cancel_event.set()
            start_event.set()
            _teardown_process_group(process, windows_job)
            with self._lock:
                self._runs.pop(run_id, None)
                self._active_runs.pop(run_id, None)
            event_queue.close()
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

    def subscribe(self) -> queue.Queue:
        subscription: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: queue.Queue) -> None:
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscription]

    def close(self) -> None:
        """Stop background services and boundedly tear down every active run."""
        with self._lock:
            first_close = not self._closed
            self._closed = True
            handles = list(self._active_runs.items())
        if first_close:
            self._watcher_stop.set()
            self._scheduler.stop()
            if self._watcher_thread is not None:
                self._watcher_thread.join(timeout=2.0)

        for _, handle in handles:
            handle.cancel_event.set()
            handle.start_event.set()
        deadline = time.monotonic() + self._cancel_grace
        for run_id, handle in handles:
            if handle.process.pid is not None:
                handle.process.join(timeout=max(0.0, deadline - time.monotonic()))
            _teardown_process_group(handle.process, handle.windows_job)
        drain_deadline = time.monotonic() + 2.0
        for run_id, handle in handles:
            if handle.drain_thread is not None:
                handle.drain_thread.join(
                    timeout=max(0.0, drain_deadline - time.monotonic())
                )
            with self._lock:
                if self._active_runs.get(run_id) is handle:
                    self._active_runs.pop(run_id, None)

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
            view = self._registry.rescan()
            self._scheduler.reconcile(view.by_id.values())

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
            if event["type"] == "prepared":
                return event, buffered
            if event["type"] == "prepare_failed":
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
                terminal = self._apply_event(run_id, event)
        finally:
            _teardown_process_group(
                handle.process, handle.windows_job, wait_before_term=2.0
            )
            with self._lock:
                self._active_runs.pop(run_id, None)
            handle.event_queue.close()

    def _apply_event(self, run_id: str, event: dict[str, Any]) -> bool:
        event_type = event["type"]
        log_entry: LogEntry | None = None
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return event_type == "terminal"
            if event_type == "running":
                if run.status != RunStatus.CANCELLED:
                    run.status = RunStatus.RUNNING
                    run.started_at = event["timestamp"]
            elif event_type.startswith("node_"):
                node = run.nodes.get(event["node_id"])
                if node is not None and run.status != RunStatus.CANCELLED:
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
                run.logs.append(log_entry)
            elif event_type == "terminal":
                if run.status != RunStatus.CANCELLED:
                    run.status = {
                        "success": RunStatus.SUCCESS,
                        "failed": RunStatus.FAILED,
                        "cancelled": RunStatus.CANCELLED,
                    }[event["status"]]
                run.ended_at = time.monotonic()
                self._skip_unfinished_nodes(run)
        if log_entry is not None:
            self._notify_log(log_entry)
        self._notify_run(run)
        return event_type == "terminal"

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
                return
            run.status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
            run.ended_at = time.monotonic()
            if not cancelled:
                run.logs.append(entry)
            self._skip_unfinished_nodes(run)
        if not cancelled:
            self._notify_log(entry)
        self._notify_run(run)

    def _force_cancel_after_grace(self, run_id: str, handle: _RunHandle) -> None:
        handle.process.join(timeout=self._cancel_grace)
        with self._lock:
            if self._active_runs.get(run_id) is not handle:
                return
        _teardown_process_group(handle.process, handle.windows_job)

    @staticmethod
    def _skip_unfinished_nodes(run: RunState) -> None:
        for node in run.nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                node.status = NodeStatus.SKIPPED

    def _notify_run(self, run: RunState) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            callback_notifications = tuple(
                (callback, deepcopy(run)) for callback in self._run_callbacks
            )
            subscriber_notifications = tuple(
                (subscription, deepcopy(run)) for subscription in self._subscribers
            )
        for callback, snapshot in callback_notifications:
            try:
                callback(snapshot)
            except Exception:
                pass
        for subscription, snapshot in subscriber_notifications:
            try:
                subscription.put_nowait((sequence, snapshot))
            except queue.Full:
                pass

    def _notify_log(self, entry: LogEntry) -> None:
        with self._lock:
            callbacks = tuple(self._log_callbacks)
        for callback in callbacks:
            try:
                callback(entry)
            except Exception:
                pass


def _resolve_executor_config(
    *,
    executor_backend: ExecutorBackend | _ExecutorBackendOmitted,
    ray_runtime_env: Mapping[str, Any] | None,
    ray_init_kwargs: Mapping[str, Any] | None,
    executor_factory: DeprecatedExecutorFactory | None,
) -> dict[str, Any]:
    backend_was_provided = not isinstance(
        executor_backend, _ExecutorBackendOmitted
    )
    backend: ExecutorBackend = (
        "local" if not backend_was_provided else executor_backend
    )
    if executor_factory is not None:
        warnings.warn(
            "executor_factory is deprecated; use executor_backend and the "
            "ray_runtime_env/ray_init_kwargs configuration instead",
            DeprecationWarning,
            stacklevel=3,
        )
        if (
            backend_was_provided
            or ray_runtime_env is not None
            or ray_init_kwargs is not None
        ):
            raise TypeError(
                "executor_factory cannot be combined with executor_backend or "
                "Ray configuration"
            )
        return _deprecated_executor_config(executor_factory)

    if backend not in {"local", "ray"}:
        raise ValueError(
            f"Unsupported executor_backend {backend!r}; expected 'local' or 'ray'"
        )
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
                "Unsupported RayExecutor partial arguments: "
                + ", ".join(sorted(unsupported))
            )
        return {
            "backend": "ray",
            "runtime_env": dict((factory.keywords or {}).get("runtime_env") or {}),
            "ray_init_kwargs": dict(
                (factory.keywords or {}).get("ray_init_kwargs") or {}
            ),
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
) -> None:
    """Boundedly stop a coordinator session and all of its descendants."""
    if process.pid is None:
        close_job(windows_job)
        return
    if wait_before_term:
        process.join(timeout=wait_before_term)
    if os.name != "nt":
        group_signalled = _signal_coordinator_group(process.pid, signal.SIGTERM)
        if not group_signalled and process.is_alive():
            process.terminate()
    elif process.is_alive():
        process.terminate()
    process.join(timeout=term_grace)
    group_alive = os.name != "nt" and _coordinator_group_exists(process.pid)
    if process.is_alive() or group_alive:
        if os.name != "nt":
            group_signalled = _signal_coordinator_group(process.pid, signal.SIGKILL)
            if not group_signalled and process.is_alive():
                process.kill()
        elif hasattr(process, "kill"):
            process.kill()
        process.join(timeout=kill_grace)
    close_job(windows_job)


def _signal_coordinator_group(process_group: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _coordinator_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True
