"""
Execution engines for Avalanche workflows.

Provides abstract interface for different execution backends:
- RayExecutor: Distributed execution with Ray
- LocalExecutor: Sequential execution for testing
- (Future: DaskExecutor, etc.)
"""

from functools import wraps
from typing import Any, Callable, Protocol

from ._async import call_sync_or_async, resolve_awaitable


def _normalize_distributed_result(value: Any) -> Any:
    """Convert ``AppendResult`` payloads into off-driver ``AppendResultHandle``s.

    Runs inside a Ray worker (via the task wrappers below). The appended frame
    is placed in the Ray object store and only a small handle (data ref +
    snapshot id + table identity) travels as the task payload, so the driver and
    downstream consumers never move the frame through the driver. Recurses
    through ``LineagedResult`` envelopes and tuple/list/dict containers,
    preserving shape and lineage.
    """
    from avalanche.types import AppendResult, AppendResultHandle, LineagedResult

    if isinstance(value, LineagedResult):
        return LineagedResult(
            _normalize_distributed_result(value.value), dict(value.lineage_vector)
        )
    if isinstance(value, AppendResult):
        import ray

        return AppendResultHandle(
            data_ref=ray.put(value.data),
            snapshot_id=value.snapshot_id,
            table_identity=value.table_identity,
        )
    if isinstance(value, tuple):
        return tuple(_normalize_distributed_result(v) for v in value)
    if isinstance(value, list):
        return [_normalize_distributed_result(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_distributed_result(v) for k, v in value.items()}
    return value


def _wrap_sync_or_async(fn: Callable) -> Callable:
    """Wrap a task function so async task bodies resolve before returning.

    Used by ``RayExecutor.submit``: also normalizes ``AppendResult`` payloads
    into off-driver handles so the control/data-plane split applies to every
    Ray task, not only the ones that request a status ref.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _normalize_distributed_result(call_sync_or_async(fn, *args, **kwargs))

    return wrapper


def _wrap_with_status(fn: Callable, user_num_returns: int) -> Callable:
    """Wrap a task so it also emits a small status marker as its last return.

    The status value is produced by the *same* task as the payload, so fetching
    only the status ref surfaces a task exception without materializing the
    payload on the driver (or in a separate worker). ``None`` on success.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = _normalize_distributed_result(call_sync_or_async(fn, *args, **kwargs))
        if user_num_returns > 1:
            return (*result, None)
        return result, None

    return wrapper


def _project_index(value: Any, index: int) -> Any:
    """Return ``value[index]`` (used as a remote projection task under Ray).

    Preserves a ``LineagedResult`` envelope so downstream Python-arg consumers
    still record the producer lineage of the selected element.
    """
    from avalanche.types import LineagedResult

    if isinstance(value, LineagedResult):
        return LineagedResult(value.value[index], dict(value.lineage_vector))
    return value[index]


class Executor(Protocol):
    """
    Abstract interface for workflow execution engines.

    Executors handle:
    - Task submission (returns refs/futures)
    - Ref resolution (automatically in submit via executor)
    - Parallel execution
    - Result fetching

    Key design: submit() accepts refs as arguments and the executor
    handles dereferencing them (Ray does this automatically, LocalExecutor
    stores and passes values directly).
    """

    def submit(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> Any | tuple[Any, ...]:
        """
        Submit a task for execution.

        Args:
            fn: Task function to execute
            *args: Positional arguments (can include refs from previous tasks)
            num_returns: Number of return values (for tuple unpacking)
            **kwargs: Keyword arguments (can include refs from previous tasks)

        Returns:
            Single ref if num_returns=1, tuple of refs if num_returns>1
        """
        ...

    def get(self, futures: list[Any]) -> list[Any]:
        """
        Fetch results from completed futures.

        Args:
            futures: List of task futures/refs to fetch

        Returns:
            List of materialized results
        """
        ...

    def submit_with_status(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> tuple[Any, Any]:
        """Submit a task and return ``(payload_ref_or_tuple, status_ref)``.

        The status ref is produced by the *same* task as the payload. Fetching
        only the status ref lets a caller observe completion/failure without
        materializing the payload on the driver (or in a separate worker).
        """
        ...

    def wait(self, futures: list[Any]) -> None:
        """Block until every future is complete, without materializing values."""
        ...

    def project(self, ref: Any, index: int) -> Any:
        """Return a ref/value for ``ref[index]`` without driver materialization.

        For distributed executors this submits a tiny projection task so the
        indexing happens in a worker; local executors index in place.
        """
        ...

    def shutdown(self) -> None:
        """Cleanup executor resources."""
        ...


class LocalExecutor:
    """
    Sequential local executor for testing and development.

    Executes tasks synchronously in the current process.
    Since execution is immediate, refs are just the actual values.

    Useful for:
    - Testing
    - Debugging
    - Small workflows
    - Local development
    """

    def submit(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> Any | tuple[Any, ...]:
        """
        Execute function immediately and return result(s).

        Args are already resolved values (since LocalExecutor executes immediately).

        Args:
            fn: Function to execute
            *args: Positional arguments
            num_returns: Number of return values to expect
            **kwargs: Keyword arguments

        Returns:
            Result if num_returns=1, tuple of results if num_returns>1
        """
        result = call_sync_or_async(fn, *args, **kwargs)
        if num_returns > 1:
            # Ensure result is a tuple of expected length
            if isinstance(result, tuple) and len(result) == num_returns:
                return result
            else:
                raise ValueError(
                    f"Function {fn.__name__} expected to return {num_returns} values, "
                    f"but returned: {result}"
                )
        return result

    def get(self, futures: list[Any]) -> list[Any]:
        """
        Fetch results from futures.

        For LocalExecutor, futures ARE the results (already computed),
        so just return them as-is.
        """
        return [resolve_awaitable(future) for future in futures]

    def submit_with_status(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> tuple[Any, Any]:
        """Execute immediately; the status is trivial (exceptions already raised).

        Local execution is synchronous, so any task exception surfaces here at
        submit time. The status value is ``None`` on success.
        """
        result = self.submit(fn, *args, num_returns=num_returns, **kwargs)
        return result, None

    def wait(self, futures: list[Any]) -> None:
        """Local values are already computed; just resolve any awaitables."""
        for future in futures:
            resolve_awaitable(future)

    def project(self, ref: Any, index: int) -> Any:
        """Index in place — local values are already materialized."""
        return resolve_awaitable(ref)[index]

    def shutdown(self) -> None:
        """No cleanup needed for local executor."""
        pass


class RayExecutor:
    """
    Distributed executor using Ray.

    Executes tasks in parallel across Ray cluster.
    Handles:
    - Automatic parallelization
    - Dependency tracking
    - Resource management
    - Fault tolerance (via Ray)

    Example:
        executor = RayExecutor()
        workflow.run(executor=executor).result()
    """

    def __init__(self, *, ray_init_kwargs: dict | None = None):
        """
        Initialize Ray executor.

        Args:
            ray_init_kwargs: Arguments to pass to ray.init()
                            If None, assumes Ray is already initialized
        """
        try:
            import ray
        except ImportError as e:
            raise ImportError(
                "Ray is required for RayExecutor. Install with: pip install ray"
            ) from e

        self.ray = ray

        if ray_init_kwargs is not None:
            if not ray.is_initialized():
                ray.init(**ray_init_kwargs)

    def submit(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> Any | tuple[Any, ...]:
        """
        Submit task to Ray.

        Args can include Ray ObjectRefs from previous tasks - Ray automatically
        waits for them and unpacks the values when the remote function executes.

        Args:
            fn: Function to execute
            *args: Positional arguments (can include ObjectRefs)
            num_returns: Number of return values (creates separate ObjectRefs for each)
            **kwargs: Keyword arguments (can include ObjectRefs)

        Returns:
            Single ObjectRef if num_returns=1, tuple of ObjectRefs if num_returns>1

        Note: Expects fn to be decorated with @ray.remote
              or will wrap it automatically.
        """
        # If function is not already a Ray remote, make it one. The wrapper makes
        # coroutine task bodies first-class by resolving them inside the worker
        # before Ray tries to serialize the task result.
        remote_fn = fn
        if not hasattr(remote_fn, "remote"):
            remote_fn = self.ray.remote(num_returns=num_returns)(
                _wrap_sync_or_async(remote_fn)
            )

        return getattr(remote_fn, "remote")(*args, **kwargs)

    def get(self, futures: list[Any]) -> list[Any]:
        """Fetch results from Ray ObjectRefs."""
        return self.ray.get(futures)

    def submit_with_status(
        self, fn: Callable, *args: Any, num_returns: int = 1, **kwargs: Any
    ) -> tuple[Any, Any]:
        """Submit a Ray task that also emits a tiny status marker.

        The task is created with ``num_returns + 1`` return values: the user
        payload(s) followed by a small status value (``None`` on success). The
        status ref lets the driver observe completion/failure without fetching
        the payload, and it is produced by the *same* task so no payload is
        deserialized in a separate worker.
        """
        if hasattr(fn, "remote"):
            raise TypeError(
                "submit_with_status expects a plain function, not a pre-decorated "
                "Ray remote; it needs to control num_returns for the status slot"
            )
        remote_fn = self.ray.remote(num_returns=num_returns + 1)(
            _wrap_with_status(fn, num_returns)
        )
        refs = getattr(remote_fn, "remote")(*args, **kwargs)
        # Ray returns a list of ObjectRefs when num_returns > 1.
        refs = list(refs)
        status_ref = refs[-1]
        payload_refs = refs[:-1]
        if num_returns == 1:
            return payload_refs[0], status_ref
        return tuple(payload_refs), status_ref

    def wait(self, futures: list[Any]) -> None:
        """Block until refs are ready, without observing task exceptions."""
        remaining = list(futures)
        while remaining:
            _ready, remaining = self.ray.wait(remaining, num_returns=1)

    def project(self, ref: Any, index: int) -> Any:
        """Submit a tiny worker-side projection returning ``ref[index]``.

        Keeps a single-return tuple/list payload in the object store: the
        indexing happens in a Ray worker rather than fetching the whole value
        to the driver.
        """
        return self.submit(_project_index, ref, index)

    def shutdown(self) -> None:
        """Shutdown Ray (optional)."""
        # Usually don't shutdown Ray as it may be shared
        # Users can call ray.shutdown() manually if needed
        pass


# Default executor factory
def get_default_executor() -> Executor:
    """
    Get the default executor.

    Tries Ray first, falls back to LocalExecutor if Ray not available.
    """
    try:
        return RayExecutor()
    except ImportError:
        return LocalExecutor()
