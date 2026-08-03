"""Spawn-safe per-run coordinator target and serializable event protocol."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
import threading
import time
import traceback
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from avalanche._agent_evidence import (
    capture_agent_evidence,
    capture_agent_log_node,
    current_agent_log_node_id,
)
from avalanche.dag import Workflow

from ..executor import Executor, LocalExecutor, RayExecutor
from .hooks import RunHooks
from .models import display_name_from_id
from .registry import agent_field_schemas_for_workflow
from .result_store import (
    ResultPublicationCancelledError,
    detach_transferred_bundle_descriptor,
    publish_workflow_result,
)
from .results import encode_workflow_result


def _import_isolated_ray() -> Any:
    """Import Ray without inheriting the caller's ``uv run`` project."""
    # The operator supplies a workflow-specific working_dir and owns the whole
    # coordinator process group. Ray's automatic uv hook would instead launch
    # workers through the caller's project environment, adding uv subprocesses
    # that can outlive Ray shutdown and prevent verified success quiescence.
    os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    return importlib.import_module("ray")


def run_worker(
    import_root: str,
    workflow_relative_module_file: str,
    builder_symbol: str,
    run_id: str,
    executor_config: dict[str, Any],
    assignment_event: Any,
    input_value: dict[str, Any] | None,
    context_value: dict[str, Any] | None,
    event_queue: Any,
    cancel_event: Any,
    start_event: Any,
    transferred_result_bundle_descriptor: Any,
    result_bundle_identity: tuple[int, int],
) -> None:
    """Detach exactly one inode-bound bundle capability and close it on exit."""
    bundle_descriptor: int | None = None
    try:
        bundle_descriptor = detach_transferred_bundle_descriptor(
            transferred_result_bundle_descriptor,
            result_bundle_identity,
        )
        _run_worker(
            import_root,
            workflow_relative_module_file,
            builder_symbol,
            run_id,
            executor_config,
            assignment_event,
            input_value,
            context_value,
            event_queue,
            cancel_event,
            start_event,
            bundle_descriptor,
            result_bundle_identity,
        )
    finally:
        if bundle_descriptor is not None:
            os.close(bundle_descriptor)


def _run_worker(
    import_root: str,
    workflow_relative_module_file: str,
    builder_symbol: str,
    run_id: str,
    executor_config: dict[str, Any],
    assignment_event: Any,
    input_value: dict[str, Any] | None,
    context_value: dict[str, Any] | None,
    event_queue: Any,
    cancel_event: Any,
    start_event: Any,
    result_bundle_descriptor: int,
    result_bundle_identity: tuple[int, int],
) -> None:
    """Import/build exactly once, prepare, wait for the parent, and execute."""
    event_queue = _SealableEventQueue(event_queue)
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass
    root = Path(import_root)
    stdout = _QueueStream(event_queue, "operator", logging.INFO)
    stderr = _QueueStream(event_queue, "operator", logging.ERROR)
    sys.stdout = stdout
    sys.stderr = stderr
    _install_log_capture(event_queue)

    executor: Executor | None = None
    ray_log_queue: Any | None = None
    ray_log_drain: threading.Thread | None = None
    terminal_event: dict[str, Any] | None = None
    try:
        if not assignment_event.wait(timeout=30.0):
            raise TimeoutError("Coordinator process ownership was not confirmed")
        os.chdir(root)
        sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        module = _import_workflow_module(root, workflow_relative_module_file)
        builder = getattr(module, builder_symbol, None)
        if (
            not callable(builder)
            or not getattr(builder, "__avalanche_workflow__", False)
            or getattr(builder, "__module__", None) != module.__name__
        ):
            raise LookupError(f"Workflow builder is unavailable: {builder_symbol}")
        workflow = builder()
        if not isinstance(workflow, Workflow):
            raise TypeError("Marked builder did not return a Workflow")
        _put_preparation_event(
            event_queue,
            {"type": "prepared", **_workflow_metadata(workflow)},
        )
    except BaseException as exc:
        _put_preparation_event(
            event_queue,
            {
                "type": "prepare_failed",
                "error": _bounded_event_text(f"{type(exc).__name__}: {exc}", 65_536),
                "traceback": _bounded_event_text(traceback.format_exc(), 262_144),
            },
        )
        return

    while not start_event.wait(0.05):
        if cancel_event.is_set():
            _put_run_event(event_queue, {"type": "terminal", "status": "cancelled"})
            return

    try:
        wrap_fn: Callable[[str, Callable[..., Any]], Callable[..., Any]]
        executor_mode = executor_config["backend"]
        if executor_mode == "local":
            executor = LocalExecutor()

            def wrap_fn(node_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
                return _with_local_node_observers(node_id, fn, stdout, stderr, event_queue)
        elif executor_mode == "ray":
            ray = _import_isolated_ray()

            if ray.is_initialized():
                raise RuntimeError(
                    "Ray must not be initialized before the run coordinator applies "
                    "the workflow runtime_env"
                )
            runtime_env = dict(executor_config.get("runtime_env") or {})
            runtime_env["working_dir"] = str(root)
            executor = RayExecutor(
                runtime_env=runtime_env,
                ray_init_kwargs=dict(executor_config.get("ray_init_kwargs") or {}),
            )
            from ray.util.queue import Queue as RayQueue

            ray_log_queue = RayQueue()
            ray_log_drain = threading.Thread(
                target=_drain_ray_logs,
                args=(ray_log_queue, event_queue),
                name=f"avalanche-ray-log-drain-{run_id}",
                daemon=True,
            )
            ray_log_drain.start()

            def wrap_fn(node_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
                return _with_ray_node_observers(node_id, fn, ray_log_queue)
        else:
            raise ValueError(f"Unknown executor mode: {executor_mode}")

        _put_run_event(event_queue, {"type": "running", "timestamp": time.monotonic()})
        hooks = RunHooks(
            on_node_start=lambda node_id: _put_run_event(
                event_queue,
                {"type": "node_started", "node_id": node_id, "timestamp": time.monotonic()},
            ),
            on_node_success=lambda node_id: _put_run_event(
                event_queue,
                {"type": "node_succeeded", "node_id": node_id, "timestamp": time.monotonic()},
            ),
            on_node_failure=lambda node_id, exc: _put_run_event(
                event_queue,
                {
                    "type": "node_failed",
                    "node_id": node_id,
                    "timestamp": time.monotonic(),
                    "error": _bounded_event_text(
                        f"{type(exc).__name__}: {exc}",
                        65_536,
                    ),
                },
            ),
            cancel_requested=cancel_event.is_set,
            wrap_fn=wrap_fn,
        )
        result = workflow.run(
            executor=executor,
            hooks=hooks,
            input=input_value,
            context=context_value,
            run_id=run_id,
        ).result()
        status = "cancelled" if cancel_event.is_set() else "success"
        terminal_event = {"type": "terminal", "status": status}
        if status == "success":
            encoded_result = encode_workflow_result(result)
            manifest_sha256 = publish_workflow_result(
                encoded_result,
                result_bundle_descriptor,
                result_bundle_identity,
                cancel_event,
            )
            if cancel_event.is_set():
                terminal_event = {"type": "terminal", "status": "cancelled"}
            else:
                terminal_event["result_manifest_sha256"] = manifest_sha256
    except ResultPublicationCancelledError:
        terminal_event = {"type": "terminal", "status": "cancelled"}
    except BaseException as exc:
        if cancel_event.is_set():
            terminal_event = {"type": "terminal", "status": "cancelled"}
        else:
            _put_run_event(
                event_queue,
                {
                    "type": "log",
                    "timestamp": time.time(),
                    "level": logging.ERROR,
                    "node_id": "operator",
                    "message": _bounded_event_text(
                        f"Run failed: {exc}\n{traceback.format_exc()}",
                        65_536,
                    ),
                },
            )
            terminal_event = {
                "type": "terminal",
                "status": "failed",
                "error": _bounded_event_text(f"{type(exc).__name__}: {exc}", 65_536),
            }
    finally:
        if executor is not None:
            executor.shutdown()
        if ray_log_queue is not None:
            try:
                ray_log_queue.put(_RAY_LOG_STOP)
            except Exception:
                pass
        if ray_log_drain is not None:
            ray_log_drain.join(timeout=5.0)
        if ray_log_queue is not None:
            try:
                ray_log_queue.shutdown(force=True)
            except Exception:
                pass
        if ray_log_drain is not None and ray_log_drain.is_alive():
            ray_log_drain.join(timeout=1.0)
        if executor_mode == "ray":
            try:
                import ray

                if ray.is_initialized():
                    ray.shutdown()
            except Exception:
                pass
        # The terminal success event is a protocol boundary: the operator
        # quiesces the coordinator and rejects every event observed after it.
        # Flush partial captured output first so interpreter shutdown cannot
        # turn pre-terminal output into a late protocol event.
        stdout.flush()
        stderr.flush()
        if terminal_event is not None:
            _put_terminal_run_event(event_queue, terminal_event)


def _import_workflow_module(root: Path, relative_file: str):
    path = root / relative_file
    if not path.is_file() or path.suffix != ".py":
        raise FileNotFoundError(f"Workflow module is unavailable: {relative_file}")
    parts = [path.stem]
    parent = path.parent
    while parent != root and (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    if parent == root and len(parts) > 1:
        module_name = ".".join(reversed(parts))
        return importlib.import_module(module_name)

    # A standalone module may be nested under a configured directory without
    # being a package. Load that exact file while allowing its directory to
    # provide sibling imports.
    module_name = f"_avalanche_run_{path.stem}_{abs(hash(relative_file))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load workflow module: {relative_file}")
    sys.path.insert(0, str(path.parent))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _workflow_metadata(workflow: Workflow) -> dict[str, Any]:
    node_ids = workflow._topological_sort()
    return {
        "display_name": workflow.name,
        "node_ids": node_ids,
        "graph": {key: list(value) for key, value in workflow.graph.items()},
        "node_types": {
            node_id: workflow.nodes[node_id].node.node_type.value for node_id in node_ids
        },
        "display_names": {node_id: display_name_from_id(node_id) for node_id in node_ids},
        "agent_field_schemas_json": agent_field_schemas_for_workflow(workflow, node_ids),
    }


class _SealableEventQueue:
    """Serialize the terminal event and reject asynchronous writes after it."""

    def __init__(self, event_queue: Any) -> None:
        self._event_queue = event_queue
        self._lock = threading.Lock()
        self._sealed = False

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._sealed:
                self._event_queue.put(event)

    def put_terminal(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._sealed:
                raise RuntimeError("coordinator event queue is already terminal")
            self._event_queue.put(event)
            self._sealed = True


def _install_log_capture(event_queue: Any) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_QueueLogHandler(event_queue))
    root.setLevel(logging.DEBUG)


class _QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: Any, node_id: str | None = None) -> None:
        super().__init__(logging.DEBUG)
        self._queue = event_queue
        self._node_id = node_id

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        node_id = (
            current_agent_log_node_id()
            or self._node_id
            or (name.split(".")[-1] if name.startswith("avalanche.node.") else name)
        )
        _put_run_event(
            self._queue,
            {
                "type": "log",
                "timestamp": record.created,
                "level": record.levelno,
                "node_id": _bounded_event_text(node_id, 4096),
                "message": _bounded_event_text(record.getMessage(), 65_536),
            },
        )


class _QueueStream:
    def __init__(self, event_queue: Any, node_id: str, level: int) -> None:
        self._queue = event_queue
        self.node_id = node_id
        self._level = level
        self._buffer = ""

    def write(self, value: str) -> int:
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit(line.rstrip())
        return len(value)

    def flush(self) -> None:
        if self._buffer.strip():
            self._emit(self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        """Expose the underlying standard fd for libraries such as Ray."""
        stream = sys.__stderr__ if self._level >= logging.ERROR else sys.__stdout__
        if stream is None:
            raise OSError("standard stream is unavailable")
        return stream.fileno()

    def _emit(self, message: str) -> None:
        _put_run_event(
            self._queue,
            {
                "type": "log",
                "timestamp": time.time(),
                "level": self._level,
                "node_id": current_agent_log_node_id() or self.node_id,
                "message": _bounded_event_text(message, 65_536),
            },
        )


def _with_local_node_observers(
    node_id: str,
    fn: Callable[..., Any],
    stdout: _QueueStream,
    stderr: _QueueStream,
    event_queue: Any,
) -> Callable[..., Any]:
    if getattr(fn, "__agent_step__", None) is not None:
        fn = _with_agent_evidence(node_id, fn, event_queue)
    return _with_node_streams(node_id, fn, stdout, stderr)


def _with_node_streams(
    node_id: str,
    fn: Callable[..., Any],
    stdout: _QueueStream,
    stderr: _QueueStream,
) -> Callable[..., Any]:
    name = node_id

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            old_stdout, old_stderr = stdout.node_id, stderr.node_id
            stdout.node_id = name
            stderr.node_id = name
            try:
                return await fn(*args, **kwargs)
            finally:
                stdout.flush()
                stderr.flush()
                stdout.node_id = old_stdout
                stderr.node_id = old_stderr

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        old_stdout, old_stderr = stdout.node_id, stderr.node_id
        stdout.node_id = name
        stderr.node_id = name
        try:
            return fn(*args, **kwargs)
        finally:
            stdout.flush()
            stderr.flush()
            stdout.node_id = old_stdout
            stderr.node_id = old_stderr

    return wrapper


def _with_agent_evidence(
    node_id: str,
    fn: Callable[..., Any],
    event_queue: Any,
) -> Callable[..., Any]:
    """Forward agent evidence through the coordinator's event protocol."""

    def emit(event: dict[str, Any]) -> None:
        try:
            event_queue.put(
                {
                    "type": "agent_evidence",
                    "node_id": node_id,
                    "event": event,
                }
            )
        except BaseException:
            pass

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with capture_agent_evidence(emit), capture_agent_log_node(node_id):
                return await fn(*args, **kwargs)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with capture_agent_evidence(emit), capture_agent_log_node(node_id):
            return fn(*args, **kwargs)

    return wrapper


_RAY_LOG_STOP = {"type": "_ray_log_stop"}


def _with_ray_node_observers(
    node_id: str,
    fn: Callable[..., Any],
    ray_log_queue: Any,
) -> Callable[..., Any]:
    if getattr(fn, "__agent_step__", None) is not None:
        fn = _with_agent_evidence(node_id, fn, ray_log_queue)
    return _with_ray_node_streams(node_id, fn, ray_log_queue)


def _with_ray_node_streams(
    node_id: str,
    fn: Callable[..., Any],
    ray_log_queue: Any,
) -> Callable[..., Any]:
    """Return a cloudpickle-safe worker wrapper using only a Ray queue."""
    name = node_id

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            stdout = _QueueStream(ray_log_queue, name, logging.INFO)
            stderr = _QueueStream(ray_log_queue, name, logging.ERROR)
            old_stdout, old_stderr = sys.stdout, sys.stderr
            root_logger = logging.getLogger()
            old_level = root_logger.level
            handler = _QueueLogHandler(ray_log_queue, node_id)
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.DEBUG)
            sys.stdout, sys.stderr = stdout, stderr
            try:
                return await fn(*args, **kwargs)
            finally:
                stdout.flush()
                stderr.flush()
                sys.stdout, sys.stderr = old_stdout, old_stderr
                root_logger.removeHandler(handler)
                root_logger.setLevel(old_level)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        stdout = _QueueStream(ray_log_queue, name, logging.INFO)
        stderr = _QueueStream(ray_log_queue, name, logging.ERROR)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        root_logger = logging.getLogger()
        old_level = root_logger.level
        handler = _QueueLogHandler(ray_log_queue, node_id)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        sys.stdout, sys.stderr = stdout, stderr
        try:
            return fn(*args, **kwargs)
        finally:
            stdout.flush()
            stderr.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)

    return wrapper


def _drain_ray_logs(ray_log_queue: Any, event_queue: Any) -> None:
    """Bridge Ray actor-backed events into the parent multiprocessing protocol."""
    from ray.util.queue import Empty

    while True:
        try:
            event = ray_log_queue.get(timeout=0.2)
        except Empty:
            continue
        except Exception:
            return
        if event == _RAY_LOG_STOP:
            return
        _put_run_event(event_queue, event)


def _put_preparation_event(event_queue: Any, event: dict[str, Any]) -> None:
    from .operator import _validate_preparation_event

    _validate_preparation_event(event)
    event_queue.put(event)


def _put_run_event(event_queue: Any, event: dict[str, Any]) -> None:
    from .operator import _validate_run_event

    _validate_run_event(event)
    event_queue.put(event)


def _put_terminal_run_event(
    event_queue: _SealableEventQueue,
    event: dict[str, Any],
) -> None:
    from .operator import _validate_run_event

    _validate_run_event(event)
    event_queue.put_terminal(event)


def _bounded_event_text(value: object, maximum_length: int) -> str:
    rendered = value if type(value) is str else str(value)
    return rendered[:maximum_length]
