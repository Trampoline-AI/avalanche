"""Operator — core workflow orchestration logic."""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import CancelledError
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from ..executor import LocalExecutor
from .hooks import RunHooks
from .models import (
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    WorkflowInfo,
    display_name_from_id,
)
from .registry import WorkflowRegistry


class RunAlreadyExistsError(ValueError):
    """Raised when a caller-owned run ID has already been reserved."""


class Operator:
    """Workflow orchestrator implementing the same interface as StateProvider.

    Discovers real @workflow functions, executes them in background threads,
    and broadcasts state updates to subscribers.
    """

    def __init__(
        self,
        workflow_paths: list[str] | None = None,
        executor_factory: Callable | None = None,
        watch: bool = True,
        schedule: bool = True,
    ) -> None:
        self._registry = WorkflowRegistry()
        self._workflow_paths = workflow_paths or []
        if self._workflow_paths:
            self._registry.scan(self._workflow_paths)

        self._executor_factory = executor_factory or LocalExecutor
        self._runs: dict[str, RunState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._subscribers: list[queue.Queue] = []
        self._sequence: int = 0
        self._lock = threading.Lock()
        self._watcher_stop = threading.Event()

        if watch and self._workflow_paths:
            self._start_watcher()

        # Start scheduler for cron-based workflows
        from .scheduler import Scheduler
        self._scheduler = Scheduler(self)
        if schedule and self._workflow_paths:
            self._scheduler.start()

    # ── StateProvider interface ──────────────────────────────────

    def list_workflows(self) -> list[WorkflowInfo]:
        workflows = self._registry.list_workflows()
        # Enrich with live schedule data from the scheduler
        for info in workflows:
            if info.cron:
                nxt = self._scheduler.next_run_time(info.cron)
                info.next_run_at = nxt.timestamp() if nxt else None
                last_ts = self._scheduler._last_triggered.get(info.name)
                info.last_run_at = last_ts
        return workflows

    def list_runs(self, flow_name: str) -> list[RunState]:
        return [r for r in self._runs.values() if r.flow_name == flow_name]

    def get_run(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def start_run(
        self,
        flow_name: str,
        triggered_by: str = "manual",
        *,
        run_id: str | None = None,
        input: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Start a new workflow run in a background thread."""
        builder = self._registry.get_builder(flow_name)
        info = next(p for p in self.list_workflows() if p.name == flow_name)

        run_id = run_id or f"run_{str(uuid4())[:8]}"
        run = RunState(
            run_id=run_id,
            flow_name=flow_name,
            status=RunStatus.PENDING,
            triggered_by=triggered_by,
        )
        for nid in info.node_ids:
            run.nodes[nid] = NodeState(
                node_id=nid,
                name=display_name_from_id(nid),
                node_type=info.node_types[nid],
                status=NodeStatus.PENDING,
            )
        cancel_event = threading.Event()
        with self._lock:
            if run_id in self._runs:
                raise RunAlreadyExistsError(f"Run {run_id} already exists")
            self._runs[run_id] = run
            self._cancel_events[run_id] = cancel_event

        t = threading.Thread(
            target=self._execute_run,
            args=(run, builder, cancel_event, input, context),
            daemon=True,
        )
        t.start()
        return run_id

    def cancel_run(self, run_id: str) -> None:
        event = self._cancel_events.get(run_id)
        if event:
            event.set()
        run = self._runs.get(run_id)
        if run and run.status == RunStatus.RUNNING:
            run.status = RunStatus.CANCELLED
            run.ended_at = time.monotonic()
            for ns in run.nodes.values():
                if ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                    ns.status = NodeStatus.SKIPPED
            self._notify_run(run)

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._run_callbacks.append(callback)

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._log_callbacks.append(callback)

    # ── Subscription (for gRPC streaming in Phase 2) ─────────────

    def subscribe(self) -> queue.Queue:
        """Returns a queue that receives (sequence, RunState) tuples."""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    # ── File watcher ──────────────────────────────────────────────

    def _start_watcher(self) -> None:
        """Start a background thread that watches workflow files for changes."""
        t = threading.Thread(target=self._watch_loop, daemon=True)
        t.start()

    def _watch_loop(self) -> None:
        """Watch workflow paths for .py file changes and re-scan on change."""
        from pathlib import Path

        from watchfiles import watch

        # Resolve watch directories (file paths → parent dir)
        watch_dirs = set()
        for p in self._workflow_paths:
            path = Path(p)
            watch_dirs.add(str(path if path.is_dir() else path.parent))

        for changes in watch(
            *watch_dirs,
            stop_event=self._watcher_stop,
            watch_filter=lambda _, path: path.endswith(".py"),
        ):
            changed_files = [path for _, path in changes]
            logging.getLogger(__name__).info(
                f"Workflow files changed: {changed_files}, re-scanning..."
            )
            self._registry.rescan()

    # ── Internal ─────────────────────────────────────────────────

    def _execute_run(
        self,
        run: RunState,
        builder: Callable,
        cancel: threading.Event,
        input: dict[str, Any] | None,
        context: dict[str, Any] | None,
    ) -> None:
        """Background thread: build workflow, run it, update state."""
        run.status = RunStatus.RUNNING
        run.started_at = time.monotonic()
        self._notify_run(run)

        executor = self._executor_factory()
        is_ray = type(executor).__name__ == "RayExecutor"

        # Log capture strategy:
        #   Local: _RunLogHandler on root logger (real-time, same process)
        #          + wrap_fn for stdout/stderr capture per-node
        #   Ray:   wrap_fn streams all output via ray.util.queue from workers
        handler = None
        if not is_ray:
            handler = _RunLogHandler(run, self)
            root = logging.getLogger()
            root.addHandler(handler)
            if root.level > logging.DEBUG:
                self._orig_root_level = root.level
                root.setLevel(logging.DEBUG)
            else:
                self._orig_root_level = None

        # For Ray: start a log drain thread using ray.util.queue
        log_queue = None
        drain_thread = None
        if is_ray:
            log_queue, drain_thread = self._start_ray_log_drain(run)

        try:
            workflow = builder()

            hooks = RunHooks(
                on_node_start=lambda nid: self._mark_node(run, nid, NodeStatus.RUNNING),
                on_node_success=lambda nid: self._mark_node(run, nid, NodeStatus.SUCCESS),
                on_node_failure=lambda nid, exc: self._mark_node(run, nid, NodeStatus.FAILED),
                cancel_requested=cancel.is_set,
            )

            if is_ray:
                hooks.wrap_fn = lambda nid, fn: _wrap_with_ray_log_streaming(nid, fn, log_queue)
            else:
                hooks.wrap_fn = lambda nid, fn: _wrap_with_stdout_capture(nid, fn, run, self)

            workflow.run(
                executor=executor,
                hooks=hooks,
                input=input,
                context=context,
                run_id=run.run_id,
            ).result()

            if cancel.is_set():
                run.status = RunStatus.CANCELLED
            else:
                run.status = RunStatus.SUCCESS
        except CancelledError:
            run.status = RunStatus.CANCELLED
        except Exception as exc:
            import traceback
            run.status = RunStatus.FAILED
            entry = LogEntry(
                timestamp=datetime.now(),
                level=LogLevel.ERROR,
                node_id="operator",
                message=f"Run failed: {exc}\n{traceback.format_exc()}",
            )
            run.logs.append(entry)
            self._notify_log(entry)
        finally:
            if handler:
                root = logging.getLogger()
                root.removeHandler(handler)
                if getattr(self, "_orig_root_level", None) is not None:
                    root.setLevel(self._orig_root_level)
            if log_queue is not None:
                # Signal drain thread to stop
                try:
                    log_queue.put(None, timeout=1)
                except Exception:
                    pass
            run.ended_at = time.monotonic()
            for ns in run.nodes.values():
                if ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                    ns.status = NodeStatus.SKIPPED
            self._notify_run(run)

    def _start_ray_log_drain(self, run: RunState):
        """Start a thread that drains log entries from a Ray queue in real-time."""
        import ray.util.queue

        log_queue = ray.util.queue.Queue()

        def _drain():
            while True:
                try:
                    item = log_queue.get(timeout=1.0)
                    if item is None:
                        break
                    level = _LEVEL_MAP.get(item.get("level", logging.INFO), LogLevel.INFO)
                    entry = LogEntry(
                        timestamp=datetime.fromtimestamp(item["ts"]),
                        level=level,
                        node_id=item.get("node", "unknown"),
                        message=item["msg"],
                    )
                    run.logs.append(entry)
                    self._notify_log(entry)
                    self._notify_run(run)
                except Exception:
                    pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return log_queue, t

    def _mark_node(self, run: RunState, node_id: str, status: NodeStatus) -> None:
        """Update a node's status and broadcast."""
        ns = run.nodes.get(node_id)
        if ns is None:
            return
        ns.status = status
        now = time.monotonic()
        if status == NodeStatus.RUNNING:
            ns.started_at = now
        elif status in (NodeStatus.SUCCESS, NodeStatus.FAILED):
            ns.ended_at = now
        self._notify_run(run)

    def _notify_run(self, run: RunState) -> None:
        """Broadcast run state to all callbacks and subscribers."""
        for cb in self._run_callbacks:
            try:
                cb(run)
            except Exception:
                pass
        with self._lock:
            self._sequence += 1
            seq = self._sequence
            for q in self._subscribers:
                try:
                    q.put_nowait((seq, run))
                except queue.Full:
                    pass

    def _notify_log(self, entry: LogEntry) -> None:
        """Broadcast a log entry to all log callbacks."""
        for cb in self._log_callbacks:
            try:
                cb(entry)
            except Exception:
                pass

    def _unwrap_logs(self, run: RunState, node_id: str, result: Any) -> Any:
        """Extract logs from a wrapped result (result, log_records) tuple."""
        if not isinstance(result, tuple) or len(result) != 2:
            return result
        actual_result, records = result
        if not isinstance(records, list):
            return result  # Not a wrapped result
        for r in records:
            if not isinstance(r, dict) or "ts" not in r:
                return result  # Not log records
            break
        # Convert raw dicts to LogEntry objects
        for r in records:
            level = _LEVEL_MAP.get(r.get("level", logging.INFO), LogLevel.INFO)
            entry = LogEntry(
                timestamp=datetime.fromtimestamp(r["ts"]),
                level=level,
                node_id=node_id.rsplit("_", 1)[0] if "_" in node_id else node_id,
                message=r["msg"],
            )
            run.logs.append(entry)
            self._notify_log(entry)
        self._notify_run(run)
        return actual_result


# ── Log capture ──────────────────────────────────────────────

_LEVEL_MAP = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARN,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.ERROR,
}


class _RunLogHandler(logging.Handler):
    """Captures log records from ALL loggers in-process (LocalExecutor).

    Installed on the root logger to catch avalanche.node.*, third-party
    loggers, and any other logging output during node execution.
    """

    def __init__(self, run: RunState, operator: Operator) -> None:
        super().__init__(level=logging.DEBUG)
        self._run = run
        self._operator = operator
        self._active = False  # guard against recursion

    def emit(self, record: logging.LogRecord) -> None:
        if self._active:
            return
        self._active = True
        try:
            # Derive node_id: "avalanche.node.fetch_data" → "fetch_data"
            # Other loggers: use the logger name as-is
            name = record.name
            if name.startswith("avalanche.node."):
                node_id = name.split(".")[-1]
            else:
                node_id = name

            level = _LEVEL_MAP.get(record.levelno, LogLevel.INFO)
            entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created),
                level=level,
                node_id=node_id,
                message=record.getMessage(),
            )
            self._run.logs.append(entry)
            self._operator._notify_log(entry)
            self._operator._notify_run(self._run)
        finally:
            self._active = False


def _wrap_with_stdout_capture(
    node_id: str,
    fn: Callable,
    run: RunState,
    operator: Operator,
) -> Callable:
    """Wrap a node function to capture print/stdout/stderr (LocalExecutor).

    Replaces sys.stdout/stderr only for the duration of the call, then
    restores. Logging is already captured by _RunLogHandler on root.
    """
    parts = node_id.rsplit("_", 1)
    name = parts[0] if len(parts) == 2 and parts[1].isdigit() else node_id

    def wrapper(*args, **kwargs):
        import sys as _sys

        orig_out, orig_err = _sys.stdout, _sys.stderr

        # Skip stdout wrapping entirely if not running in a real terminal.
        # pytest, ray workers, and other environments replace stdout with
        # objects that segfault when wrapped with a Python proxy.
        if not hasattr(orig_out, "isatty") or not orig_out.isatty():
            return fn(*args, **kwargs)

        class _Tee:
            def __init__(self, original, source, level):
                self._orig = original
                self._source = source
                self._level = level
                self._buf = ""

            def write(self, s):
                self._orig.write(s)
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        entry = LogEntry(
                            timestamp=datetime.now(),
                            level=self._level,
                            node_id=name,
                            message=line,
                        )
                        run.logs.append(entry)
                        operator._notify_log(entry)
                        operator._notify_run(run)
                return len(s)

            def flush(self):
                self._orig.flush()

            def __getattr__(self, attr):
                return getattr(self._orig, attr)

        _sys.stdout = _Tee(orig_out, "stdout", LogLevel.INFO)
        _sys.stderr = _Tee(orig_err, "stderr", LogLevel.ERROR)
        try:
            return fn(*args, **kwargs)
        finally:
            _sys.stdout = orig_out
            _sys.stderr = orig_err

    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    return wrapper


def _wrap_with_log_capture(node_id: str, fn: Callable) -> Callable:
    """Wrap a function to capture log records inside the worker process.

    Returns a wrapper that installs a handler on avalanche.node.{name},
    runs the function, and returns (result, log_records).
    Works in any process — local or Ray worker.
    """
    # Derive the logger name from node_id: "fetch_data_1" → "fetch_data"
    parts = node_id.rsplit("_", 1)
    name = parts[0] if len(parts) == 2 and parts[1].isdigit() else node_id

    def wrapper(*args, **kwargs):
        import logging as _logging

        records: list[dict] = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                records.append({
                    "ts": record.created,
                    "level": record.levelno,
                    "msg": record.getMessage(),
                })

        logger = _logging.getLogger(f"avalanche.node.{name}")
        handler = _Capture(level=_logging.DEBUG)
        logger.addHandler(handler)
        logger.setLevel(_logging.DEBUG)
        try:
            result = fn(*args, **kwargs)
        finally:
            logger.removeHandler(handler)
        return result, records

    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    return wrapper


def _wrap_with_ray_log_streaming(node_id: str, fn: Callable, log_queue) -> Callable:
    """Wrap a function to stream ALL output to a Ray queue in real-time.

    Captures: all loggers (root handler), print/stdout, stderr.
    The function's return value is NOT modified.
    """

    def wrapper(*args, **kwargs):
        import logging as _logging
        import sys as _sys

        def _put(node, level, msg):
            try:
                log_queue.put({
                    "ts": __import__("time").time(),
                    "level": level,
                    "node": node,
                    "msg": msg,
                }, timeout=1)
            except Exception:
                pass

        # Capture all loggers via root handler
        class _StreamHandler(_logging.Handler):
            _active = False

            def emit(self, record):
                if self._active:
                    return
                self._active = True
                try:
                    n = record.name
                    node = n.split(".")[-1] if n.startswith("avalanche.node.") else n
                    _put(node, record.levelno, record.getMessage())
                finally:
                    self._active = False

        handler = _StreamHandler(level=_logging.DEBUG)
        root = _logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(_logging.DEBUG)

        # Capture stdout/stderr
        class _QueueWriter:
            def __init__(self, original, source, level):
                self._original = original
                self._source = source
                self._level = level
                self._buf = ""

            def write(self, s):
                self._original.write(s)
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        _put(self._source, self._level, line)
                return len(s)

            def flush(self):
                self._original.flush()

            def __getattr__(self, attr):
                return getattr(self._original, attr)

        orig_out, orig_err = _sys.stdout, _sys.stderr
        _sys.stdout = _QueueWriter(orig_out, "stdout", _logging.INFO)
        _sys.stderr = _QueueWriter(orig_err, "stderr", _logging.ERROR)

        try:
            return fn(*args, **kwargs)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
            _sys.stdout = orig_out
            _sys.stderr = orig_err

    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    return wrapper
