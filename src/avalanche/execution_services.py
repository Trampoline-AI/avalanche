"""Provider-neutral worker lifecycle services for workflow execution."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from .input_ref import InputRef

EXECUTION_SERVICES_V1 = "avalanche.execution-services/v1"
_SERVICE_METHOD_NAMES = (
    "probe",
    "negotiate",
    "open",
    "materialize_input",
    "finalize",
    "abort",
    "teardown",
)


@dataclass(frozen=True)
class ExecutionTaskSpec:
    """Immutable identity for one workflow task submitted to an executor."""

    run_id: str
    workflow_name: str
    node_id: str
    node_name: str
    node_slug: str
    executor_type: str


@runtime_checkable
class ExecutionServices(Protocol):
    """Worker-side lifecycle implemented by an execution-services provider.

    Implementations must be serializable and must acquire credentials and other
    worker-local capabilities inside these methods. ``request`` is an opaque,
    immutable provider descriptor owned by the executor; it must not contain
    credentials, user-facing storage URIs, absolute local paths, open handles,
    actors, or scheduler-affinity tokens.
    """

    def probe(self, *, request: Any, task: ExecutionTaskSpec) -> Any:
        """Inspect worker capabilities without allocating task resources."""
        ...

    def negotiate(self, *, request: Any, task: ExecutionTaskSpec, probe: Any) -> Any:
        """Select a supported service mode before resources are opened."""
        ...

    def open(
        self,
        *,
        request: Any,
        task: ExecutionTaskSpec,
        negotiation: Any,
        upstream_receipts: tuple[Any, ...],
    ) -> Any:
        """Open the task-scoped service session after negotiation succeeds."""
        ...

    def materialize_input(
        self,
        *,
        session: Any,
        input_type: type | None,
        input: Any,
    ) -> Any:
        """Materialize the worker-visible run input for the consuming task.

        Materialization may be eager or lazy. The returned input must expose
        every value and path required by the task, while backing bytes may be
        fetched on first access by a provider-owned filesystem.
        """
        ...

    def finalize(self, *, session: Any) -> Any:
        """Commit the successful task session and return a small receipt."""
        ...

    def abort(self, *, session: Any, error: BaseException) -> None:
        """Abort an opened session after task or finalization failure."""
        ...

    def teardown(self, *, session: Any) -> None:
        """Release an opened session exactly once."""
        ...


@dataclass(frozen=True)
class ExecutionServicesSpec:
    """Versioned executor-owned service request for a workflow run."""

    service: ExecutionServices
    request: Any
    version: str = EXECUTION_SERVICES_V1

    def __post_init__(self) -> None:
        if self.version != EXECUTION_SERVICES_V1:
            raise ValueError(
                f"Unsupported execution services version {self.version!r}; "
                f"expected {EXECUTION_SERVICES_V1!r}"
            )
        if not isinstance(self.service, ExecutionServices):
            raise TypeError("service must implement the ExecutionServices protocol")
        async_methods = [
            name
            for name in _SERVICE_METHOD_NAMES
            if inspect.iscoroutinefunction(getattr(self.service, name))
        ]
        if async_methods:
            raise TypeError(
                "ExecutionServices lifecycle methods must be synchronous; "
                f"async methods: {', '.join(async_methods)}"
            )


@dataclass(frozen=True)
class ExecutionServiceReceipt:
    """A terminal task receipt made available through ``RunHandle``."""

    node_id: str
    node_slug: str
    value: Any


def _resolve_input_ref(value: Any, run_input: Any, task: ExecutionTaskSpec) -> Any:
    if not isinstance(value, InputRef):
        return value
    if run_input is None:
        raise ValueError(
            f"Node {task.node_name!r} references {value!r} in workflow "
            f"{task.workflow_name!r}, but the execution service returned no run input"
        )

    current = run_input
    for attr in value.path:
        try:
            current = getattr(current, attr)
        except AttributeError as exc:
            raise AttributeError(
                f"Node {task.node_name!r} references {value!r} in workflow "
                f"{task.workflow_name!r}, but attribute {attr!r} is missing on "
                f"{type(current).__name__}."
            ) from exc
    return current


def _coerce_materialized_input(input_type: type | None, value: Any) -> Any:
    if input_type is None or isinstance(value, input_type):
        return value
    from .runtime import BaseInput

    if not isinstance(input_type, type) or not issubclass(input_type, BaseInput):
        raise TypeError("workflow input type must inherit from ava.BaseInput")
    return input_type.model_validate(value)


def _call_service_method(method: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Call one synchronous provider method and reject hidden awaitables."""
    value = method(**kwargs)
    if not inspect.isawaitable(value):
        return value
    if inspect.iscoroutine(value):
        value.close()
    else:
        cancel = getattr(value, "cancel", None)
        if callable(cancel):
            cancel()
    raise TypeError(
        "ExecutionServices lifecycle methods must be synchronous; "
        f"{getattr(method, '__name__', type(method).__name__)} returned an awaitable"
    )


def _add_cleanup_note(error: BaseException, phase: str, cleanup_error: BaseException) -> None:
    error.add_note(
        f"Execution services {phase} failed while preserving the primary error: "
        f"{cleanup_error!r}"
    )


def _run_with_execution_services(
    fn: Any,
    spec: ExecutionServicesSpec,
    task: ExecutionTaskSpec,
    input_type: type | None,
    raw_input: Any,
    input_param_names: tuple[str, ...],
    upstream_receipts: tuple[Any, ...],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    num_returns: int,
    normalize_result: Callable[[Any], Any] | None = None,
) -> tuple[Any, Any]:
    """Run the service lifecycle around one actual user task invocation."""
    from runtime._async import call_sync_or_async

    service = spec.service
    probe = _call_service_method(service.probe, request=spec.request, task=task)
    negotiation = _call_service_method(
        service.negotiate,
        request=spec.request,
        task=task,
        probe=probe,
    )
    session = _call_service_method(
        service.open,
        request=spec.request,
        task=task,
        negotiation=negotiation,
        upstream_receipts=upstream_receipts,
    )

    finalized = False
    primary_error: BaseException | None = None
    try:
        materialized_input = _call_service_method(
            service.materialize_input,
            session=session,
            input_type=input_type,
            input=raw_input,
        )
        materialized_input = _coerce_materialized_input(input_type, materialized_input)
        resolved_args = tuple(
            _resolve_input_ref(value, materialized_input, task) for value in args
        )
        resolved_kwargs = {
            name: _resolve_input_ref(value, materialized_input, task)
            for name, value in kwargs.items()
        }
        for name in input_param_names:
            resolved_kwargs[name] = materialized_input

        result = call_sync_or_async(fn, *resolved_args, **resolved_kwargs)
        if normalize_result is not None:
            result = normalize_result(result)
        if num_returns > 1 and (
            not isinstance(result, (tuple, list)) or len(result) != num_returns
        ):
            raise ValueError(
                f"Function {fn.__name__} expected to return {num_returns} values, "
                f"but returned: {result}"
            )
        receipt = _call_service_method(service.finalize, session=session)
        finalized = True
        return result, receipt
    except BaseException as error:
        primary_error = error
        if not finalized:
            try:
                _call_service_method(service.abort, session=session, error=error)
            except BaseException as abort_error:
                _add_cleanup_note(error, "abort", abort_error)
        raise
    finally:
        try:
            _call_service_method(service.teardown, session=session)
        except BaseException as teardown_error:
            if primary_error is None:
                raise
            _add_cleanup_note(primary_error, "teardown", teardown_error)


__all__ = [
    "EXECUTION_SERVICES_V1",
    "ExecutionServiceReceipt",
    "ExecutionServices",
    "ExecutionServicesSpec",
    "ExecutionTaskSpec",
]
