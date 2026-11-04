"""Execution engine contracts and concurrent in-process workflow execution."""

from typing import Any, Callable, Protocol

from ._async import call_sync_or_async, resolve_awaitable


class Executor(Protocol):
    """
    Abstract interface for workflow execution engines.

    Executors handle:
    - Task submission (returns refs/futures)
    - Ref resolution (automatically in submit via executor)
    - Parallel execution
    - Result fetching

    Key design: submit() accepts refs as arguments and the executor
    handles dereferencing them. LocalExecutor stores and passes values directly.
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

    def submit_with_services(
        self,
        fn: Callable,
        execution_services: Any,
        task: Any,
        input_type: type | None,
        run_input: Any,
        input_param_names: tuple[str, ...],
        receipt_dependencies: tuple[Any, ...],
        num_returns: int,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any | None]:
        """Return separate payload, receipt, and optional status channels."""
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
    In-process executor for testing and local development.

    Direct ``submit`` calls execute synchronously and return materialized values.
    ``Workflow.run`` uses ``max_workers`` to execute dependency-ready nodes in a
    thread pool while preserving DAG ordering. ``max_workers=1`` uses the
    synchronous scheduler: it checks cancellation before each node admission
    and never queues a ready sibling behind a node that may end the run.

    Useful for:
    - Testing
    - Debugging
    - I/O-bound workflows
    - Local development
    """

    max_workers: int | None = None

    def __init__(self, *, max_workers: int | None = None) -> None:
        if max_workers is not None:
            if type(max_workers) is not int:
                raise TypeError("max_workers must be an integer or None")
            if max_workers <= 0:
                raise ValueError("max_workers must be greater than zero")
        self.max_workers = max_workers

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
        from avalanche.workspace import run_workspace_invocation

        result = run_workspace_invocation(call_sync_or_async, fn, *args, **kwargs)
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

    def submit_with_services(
        self,
        fn: Callable,
        execution_services: Any,
        task: Any,
        input_type: type | None,
        run_input: Any,
        input_param_names: tuple[str, ...],
        receipt_dependencies: tuple[Any, ...],
        num_returns: int,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, Any, Any | None]:
        """Execute a service-managed task synchronously."""
        from avalanche.execution_services import _run_with_execution_services
        from avalanche.workspace import run_workspace_invocation

        result, receipt = run_workspace_invocation(
            _run_with_execution_services,
            fn,
            execution_services,
            task,
            input_type,
            run_input,
            input_param_names,
            receipt_dependencies,
            args,
            kwargs,
            num_returns=num_returns,
        )
        return result, receipt, None

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


def get_default_executor() -> LocalExecutor:
    """Create the default in-process executor."""
    return LocalExecutor()
