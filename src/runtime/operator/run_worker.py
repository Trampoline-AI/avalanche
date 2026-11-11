"""Spawn-safe per-run coordinator target and serializable event protocol."""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import sys
import threading
import time
import traceback
from contextvars import ContextVar, Token
from functools import wraps
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic_core import PydanticSerializationError, to_jsonable_python

from avalanche._agent_evidence import (
    AgentEvidenceObserverEvent,
    capture_agent_evidence,
    capture_agent_log_node,
    current_agent_log_node_id,
)
from avalanche.classifier import capture_classifier_evidence
from avalanche.classifier.models import ClassifierInvocation
from avalanche.dag import Workflow
from avalanche.evaluation_capture import capture_evaluations
from avalanche.machine import Machine
from avalanche.policies import Event
from avalanche.run_handle import RunHandle
from avalanche.runtime import BaseInput

from ..executor import LocalExecutor
from .evaluation_worker import EvaluationRequest, snapshot_evaluation
from .hooks import RunHooks
from .machine_visit import qualify_visit
from .models import display_name_from_id
from .registry import (
    agent_field_schemas_for_workflow,
    agent_instruction_lines_for_workflow,
    agent_runtime_kinds_for_workflow,
    classifier_metadata_for_workflow,
    evaluation_metadata_for_workflow,
)
from .result_store import (
    ResultPublicationCancelledError,
    detach_transferred_bundle_descriptor,
    publish_workflow_result,
)
from .results import encode_workflow_result
from .state.machines import InstanceRecord
from .step_io import preview_step_inputs, preview_value, step_signatures
from .wait_control import FinalWaitSnapshot, serve_wait_controls
from .workflow_metadata import (
    MAX_PREPARED_INPUT_LENGTH,
    PreparedRunInput,
    input_declaration_for_workflow,
    map_specs_for_workflow,
    node_docstring_lines_for_workflow,
    step_interface_for_workflow,
)

_log_attempt: ContextVar[str] = ContextVar("operator_log_attempt", default="")
_log_node: ContextVar[str] = ContextVar("operator_log_node", default="")


def run_worker(
    import_root: str,
    workflow_relative_module_file: str,
    builder_symbol: str,
    run_id: str,
    assignment_event: Any,
    input_value: dict[str, Any] | None,
    context_value: dict[str, Any] | None,
    event_queue: Any,
    cancel_event: Any,
    start_event: Any,
    transferred_result_bundle_descriptor: Any,
    result_bundle_identity: tuple[int, int],
    control_connection: Connection | None = None,
    cancel_reader: Connection | None = None,
    machine_visit: InstanceRecord | None = None,
    machine_reader: Connection | None = None,
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
            assignment_event,
            input_value,
            context_value,
            event_queue,
            cancel_event,
            start_event,
            bundle_descriptor,
            result_bundle_identity,
            control_connection,
            cancel_reader,
            machine_visit,
            machine_reader,
        )
    finally:
        if bundle_descriptor is not None:
            os.close(bundle_descriptor)


def _run_worker(
    import_root: str,
    workflow_relative_module_file: str,
    builder_symbol: str,
    run_id: str,
    assignment_event: Any,
    input_value: dict[str, Any] | None,
    context_value: dict[str, Any] | None,
    event_queue: Any,
    cancel_event: Any,
    start_event: Any,
    result_bundle_descriptor: int,
    result_bundle_identity: tuple[int, int],
    control_connection: Connection | None = None,
    cancel_reader: Connection | None = None,
    machine_visit: InstanceRecord | None = None,
    machine_reader: Connection | None = None,
) -> None:
    """Import/build exactly once, prepare, wait for the parent, and execute."""
    from avalanche.references import configure_local_run

    configure_local_run(run_id)
    event_queue = _SealableEventQueue(event_queue)
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass
    root = Path(import_root)
    _log_attempt.set(uuid4().hex)
    stdout = _QueueStream(event_queue, "operator", logging.INFO)
    stderr = _QueueStream(event_queue, "operator", logging.ERROR)
    sys.stdout = stdout
    sys.stderr = stderr
    _install_log_capture(event_queue)

    executor: LocalExecutor | None = None
    terminal_event: dict[str, Any] | None = None
    run_handle: RunHandle | None = None
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
        if machine_visit is not None:
            if not isinstance(builder, Machine):
                raise TypeError("Machine builder was replaced by an ordinary workflow")
            if machine_visit.state not in builder.spec.states:
                raise ValueError("state_missing")
            workflow = qualify_visit(
                builder.works[builder.spec.states[machine_visit.state].work],
                machine_visit.visit,
            )
        if not isinstance(workflow, Workflow):
            raise TypeError("Marked builder did not return a Workflow")
        validated_input: BaseInput | None = None
        prepared_input: PreparedRunInput | None = None
        if workflow.input_type is not None:
            # Validate once here so the run executes exactly the values it reports.
            validated_input = workflow.input_type.model_validate(
                {} if input_value is None else input_value
            )
            prepared_input = _display_copy(validated_input)
        _put_preparation_event(
            event_queue,
            {
                "type": "prepared",
                **_workflow_metadata(workflow),
                "input": None if prepared_input is None else prepared_input.model_dump(),
            },
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
        executor = LocalExecutor()

        # The operator graph displays a decision map as one declared node.
        # Keep worker/journal identities per item, but scope operator previews
        # and lifecycle observations to that group. Never relax the operator
        # protocol's rejection of genuinely unpublished node identities.
        item_groups = {
            f"{node_id}[{index}]": node_id
            for node_id, spec in workflow.node_waits.items()
            if spec.map_max_items is not None
            for index in range(spec.map_max_items)
        }
        show_groups = {
            f"{node_id}.show[{index}]": node_id
            for node_id, spec in workflow.node_waits.items()
            if spec.map_max_items is not None
            for index in range(spec.map_max_items)
        }

        show_groups.update(
            {
                f"{node_id}.show": node_id
                for node_id, spec in workflow.node_waits.items()
                if spec.map_max_items is not None
            }
        )

        def wrap_fn(node_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
            owner = show_groups.get(node_id, node_id)
            return _with_local_node_observers(owner, fn, stdout, stderr, event_queue)

        _put_run_event(event_queue, {"type": "running", "timestamp": time.monotonic()})
        from .machine_steps import MachineStepPending

        machine_pending = MachineStepPending()

        def machine_step(request):
            future = machine_pending.register(request)
            _put_run_event(
                event_queue, {"type": "machine_step", "request_json": request.model_dump_json()}
            )
            return future

        hooks = RunHooks(
            machine_step=machine_step,
            machine_payload=machine_visit.payload if machine_visit is not None else None,
            on_nodes_expanded=lambda expansion: (
                None
                if expansion.parent in show_groups
                else _put_run_event(
                    event_queue,
                    {"type": "nodes_expanded", "expansion": expansion.model_dump_json()},
                )
            ),
            on_node_start=lambda node_id: (
                None
                if node_id in show_groups or node_id in item_groups
                else _put_run_event(
                    event_queue,
                    {"type": "node_started", "node_id": node_id, "timestamp": time.monotonic()},
                )
            ),
            on_node_success=lambda node_id: (
                None
                if node_id in show_groups or node_id in item_groups
                else _put_run_event(
                    event_queue,
                    {
                        "type": "node_succeeded",
                        "node_id": node_id,
                        "timestamp": time.monotonic(),
                    },
                )
            ),
            on_node_failure=lambda node_id, exc: (
                None
                if node_id in item_groups
                else _put_run_event(
                    event_queue,
                    {
                        "type": "node_failed",
                        "node_id": show_groups.get(node_id, node_id),
                        "timestamp": time.monotonic(),
                        "error": _bounded_event_text(
                            f"{type(exc).__name__}: {exc}",
                            65_536,
                        ),
                    },
                )
            ),
            cancel_requested=cancel_event.is_set,
            wrap_fn=wrap_fn,
        )
        run_handle = workflow.run(
            executor=executor,
            hooks=hooks,
            input=input_value if validated_input is None else validated_input,
            context=context_value,
            run_id=run_id,
        )
        if control_connection is not None:
            serve_wait_controls(
                run_handle, control_connection, cancel_reader, machine_reader, machine_pending
            )
        result = run_handle.result()
        from avalanche.references import LocalWorkspaceStore

        LocalWorkspaceStore.for_run(run_id).commit_mounts()
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
                if machine_visit is not None:
                    terminal_event["machine_outcome_json"] = json.dumps(
                        {
                            "event": result.name if isinstance(result, Event) else None,
                            "output": to_jsonable_python(
                                result.output if isinstance(result, Event) else result
                            ),
                        },
                        allow_nan=False,
                    )
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
        # The terminal success event is a protocol boundary: the operator
        # quiesces the coordinator and rejects every event observed after it.
        # Flush partial captured output first so interpreter shutdown cannot
        # turn pre-terminal output into a late protocol event.
        stdout.flush()
        stderr.flush()
        if terminal_event is not None:
            if run_handle is not None:
                try:
                    run_handle.waits.close()
                    _put_run_event(
                        event_queue,
                        {
                            "type": "wait_snapshot",
                            "snapshot": FinalWaitSnapshot.capture(run_handle).model_dump_json(),
                        },
                    )
                except Exception as exc:
                    # Export is a required protocol boundary, not best-effort
                    # display data. Report its failure and the original outcome
                    # explicitly instead of exiting without a terminal event.
                    error = _bounded_event_text(
                        f"Wait snapshot export failed after {terminal_event['status']}: "
                        f"{type(exc).__name__}: {exc}; "
                        f"original error: {terminal_event.get('error', '')}",
                        65_536,
                    )
                    _put_run_event(
                        event_queue,
                        {
                            "type": "log",
                            "timestamp": time.time(),
                            "level": logging.ERROR,
                            "node_id": "operator",
                            "message": error,
                        },
                    )
                    terminal_event = {"type": "terminal", "status": "failed", "error": error}
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


def _display_copy(validated_input: BaseInput) -> PreparedRunInput | None:
    """Report the run's input values; values that cannot be shown never block the run."""
    try:
        # Field names, not aliases, key both the values and the declared schema.
        values_json = validated_input.model_dump_json(by_alias=False)
    except PydanticSerializationError:
        return None
    if len(values_json) > MAX_PREPARED_INPUT_LENGTH:
        return None
    return PreparedRunInput(
        values_json=values_json,
        supplied_fields=tuple(sorted(validated_input.model_fields_set)),
    )


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
        "agent_instruction_lines": agent_instruction_lines_for_workflow(workflow, node_ids),
        "agent_runtime_kinds": agent_runtime_kinds_for_workflow(workflow, node_ids),
        "classifier_metadata_json": classifier_metadata_for_workflow(workflow, node_ids),
        "evaluation_metadata_json": evaluation_metadata_for_workflow(workflow, node_ids),
        "step_interface_json": step_interface_for_workflow(workflow, node_ids),
        "map_specs": {
            key: spec.model_dump(mode="json")
            for key, spec in map_specs_for_workflow(workflow, node_ids).items()
        },
        "input_declaration_json": input_declaration_for_workflow(workflow, node_ids),
        "standard_step_docstring_lines": node_docstring_lines_for_workflow(workflow, node_ids),
    }


class _SealableEventQueue:
    """Serialize the terminal event and reject asynchronous writes after it."""

    def __init__(self, event_queue: Any) -> None:
        self._event_queue = event_queue
        self._lock = threading.Lock()
        self._sealed = False
        self._log_records = 0
        self._log_bytes = 0
        self._log_truncated = False

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._sealed:
                return
            if event["type"] == "log":
                size = len(event["message"].encode())
                if self._log_records >= 10_000 or self._log_bytes + size > 4 * 1024 * 1024:
                    if not self._log_truncated:
                        self._event_queue.put(
                            {
                                **event,
                                "message": "[activity truncated: worker log budget exhausted]",
                            }
                        )
                        self._log_truncated = True
                    return
                self._log_records += 1
                self._log_bytes += size
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
    root.setLevel(logging.INFO)


class _QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: Any, node_id: str | None = None) -> None:
        super().__init__(logging.INFO)
        self._queue = event_queue
        self._node_id = node_id

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        node_id = (
            current_agent_log_node_id()
            or _log_node.get()
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
                "message": _bounded_log_text(record.getMessage()),
                "channel": "log",
                "attempt": _log_attempt.get(),
            },
        )


class _QueueStream:
    def __init__(self, event_queue: Any, node_id: str, level: int) -> None:
        self._queue = event_queue
        self._default_node_id = node_id
        self._level = level
        self._node: ContextVar[str] = ContextVar("operator_stream_node", default=node_id)
        self._buffer: ContextVar[str] = ContextVar("operator_stream_buffer", default="")

    def bind_node(self, node_id: str) -> Token[str]:
        """Bind writes to the current task/thread, including concurrent async nodes."""
        return self._node.set(node_id)

    def reset_node(self, binding: Token[str]) -> None:
        self._node.reset(binding)

    def write(self, value: str) -> int:
        buffer = self._buffer.get() + value
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.strip():
                self._emit(line.rstrip())
        if len(buffer.encode()) > 8192:
            self._emit(buffer)
            buffer = ""
        self._buffer.set(buffer)
        return len(value)

    def flush(self) -> None:
        buffer = self._buffer.get()
        if buffer.strip():
            self._emit(buffer.rstrip())
        self._buffer.set("")

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        """Expose the underlying standard file descriptor."""
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
                "node_id": current_agent_log_node_id() or _log_node.get() or self._node.get(),
                "message": _bounded_log_text(message),
                "channel": "stderr" if self._level >= logging.ERROR else "stdout",
                "attempt": _log_attempt.get(),
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
        fn = _with_evaluations(node_id, fn, event_queue)
    if getattr(fn, "__classifier_step__", None) is not None:
        fn = _with_classifier_evidence(node_id, fn, event_queue)
    fn = _with_step_io(node_id, fn, event_queue)
    fn = _with_step_result(node_id, fn, event_queue)
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
            attempt_token = _log_attempt.set(uuid4().hex)
            node_token = _log_node.set(name)
            stdout_binding = stdout.bind_node(name)
            stderr_binding = stderr.bind_node(name)
            try:
                return await fn(*args, **kwargs)
            finally:
                stdout.flush()
                stderr.flush()
                stdout.reset_node(stdout_binding)
                stderr.reset_node(stderr_binding)
                _log_attempt.reset(attempt_token)
                _log_node.reset(node_token)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempt_token = _log_attempt.set(uuid4().hex)
        node_token = _log_node.set(name)
        stdout_binding = stdout.bind_node(name)
        stderr_binding = stderr.bind_node(name)
        try:
            return fn(*args, **kwargs)
        finally:
            stdout.flush()
            stderr.flush()
            stdout.reset_node(stdout_binding)
            stderr.reset_node(stderr_binding)
            _log_attempt.reset(attempt_token)
            _log_node.reset(node_token)

    return wrapper


def _with_agent_evidence(
    node_id: str,
    fn: Callable[..., Any],
    event_queue: Any,
) -> Callable[..., Any]:
    """Forward agent evidence through the coordinator's event protocol."""

    def emit(event: AgentEvidenceObserverEvent) -> None:
        # Sent as JSON: the operator validates each body once, in strict JSON mode.
        event_queue.put(
            {
                "type": "agent_evidence",
                "node_id": node_id,
                "event": event.model_dump_json(),
            }
        )

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with capture_agent_evidence(emit, errors="raise"), capture_agent_log_node(node_id):
                return await fn(*args, **kwargs)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with capture_agent_evidence(emit, errors="raise"), capture_agent_log_node(node_id):
            return fn(*args, **kwargs)

    return wrapper


class _RunEventQueue(Protocol):
    def put(self, event: dict[str, object]) -> None: ...


def _with_classifier_evidence(
    node_id: str,
    fn: Callable[..., object],
    event_queue: _RunEventQueue,
) -> Callable[..., object]:
    """Publish owned, typed invocation snapshots before returning to user code."""

    def emit(invocation: ClassifierInvocation) -> None:
        event: dict[str, object] = {
            "type": "classifier_evidence",
            "node_id": node_id,
            "event": invocation.model_dump(mode="json"),
        }
        event_queue.put(event)

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            with capture_classifier_evidence(emit):
                return await fn(*args, **kwargs)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        with capture_classifier_evidence(emit):
            return fn(*args, **kwargs)

    return wrapper


def _with_evaluations(
    node_id: str,
    fn: Callable[..., object],
    event_queue: _RunEventQueue,
) -> Callable[..., object]:
    """Snapshot at the successful agent return; never evaluate on the scheduler."""

    def publish(request: EvaluationRequest) -> None:
        event_queue.put(
            {
                "type": "evaluation_submitted",
                "node_id": node_id,
                "submission": request.model_dump(),
            }
        )

    def report_errors(errors: list[str]) -> None:
        for error in errors:
            try:
                publish(
                    EvaluationRequest(
                        evaluation_id=uuid4().hex,
                        created_at=time.time(),
                        error=f"Evaluation submission failed: {error}",
                    )
                )
            except BaseException:
                # A broken coordinator transport cannot report its own failure.
                # Capture already logged the handoff failure; do not recursively
                # write to that same transport or affect the step's return.
                pass

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            with capture_evaluations(
                lambda value: publish(snapshot_evaluation(value))
            ) as errors:
                try:
                    return await fn(*args, **kwargs)
                finally:
                    report_errors([error.error for error in errors])

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        with capture_evaluations(lambda value: publish(snapshot_evaluation(value))) as errors:
            try:
                return fn(*args, **kwargs)
            finally:
                report_errors([error.error for error in errors])

    return wrapper


def _with_step_result(
    node_id: str, fn: Callable[..., object], event_queue: _RunEventQueue
) -> Callable[..., object]:
    def publish(value: object) -> None:
        event_queue.put(
            {
                "type": "step_result",
                "node_id": node_id,
                "value_json": encode_workflow_result(value).value_json,
            }
        )

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            result = await fn(*args, **kwargs)
            publish(result)
            return result

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        result = fn(*args, **kwargs)
        publish(result)
        return result

    return wrapper


def _with_step_io(
    node_id: str,
    fn: Callable[..., object],
    event_queue: _RunEventQueue,
) -> Callable[..., object]:
    """Publish bounded previews of a standard step's arguments and return value.

    Agent and classifier steps already retain their own typed inputs and outputs.
    The previews are built where the call runs, from values the call already holds.
    """
    if (
        getattr(fn, "__agent_step__", None) is not None
        or getattr(fn, "__classifier_step__", None) is not None
    ):
        return fn
    signatures = step_signatures(fn)

    def publish_inputs(args: tuple[object, ...], kwargs: dict[str, object]) -> None:
        inputs = preview_step_inputs(signatures, args, kwargs)
        event_queue.put(
            {"type": "step_inputs", "node_id": node_id, "inputs": inputs.model_dump_json()}
        )

    def publish_output(value: object) -> None:
        output = preview_value(value)
        event_queue.put(
            {"type": "step_output", "node_id": node_id, "output": output.model_dump_json()}
        )

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            publish_inputs(args, kwargs)
            result = await fn(*args, **kwargs)
            publish_output(result)
            return result

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        publish_inputs(args, kwargs)
        result = fn(*args, **kwargs)
        publish_output(result)
        return result

    return wrapper


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


def _bounded_log_text(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= 8192:
        return value
    return raw[:8100].decode("utf-8", "ignore") + " … [record truncated]"


def _bounded_event_text(value: object, maximum_length: int) -> str:
    rendered = value if type(value) is str else str(value)
    return rendered[:maximum_length]
