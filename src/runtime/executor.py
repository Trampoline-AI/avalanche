"""
Execution engines for Avalanche workflows.

Provides abstract interface for different execution backends:
- RayExecutor: Distributed execution with Ray
- LocalExecutor: Sequential execution for testing
- (Future: DaskExecutor, etc.)
"""

from typing import Any, Callable, Protocol


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
        result = fn(*args, **kwargs)
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
        return futures

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
        workflow.run(executor=executor)
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
        # If function is not already a Ray remote, make it one
        if not hasattr(fn, "remote"):
            fn = self.ray.remote(num_returns=num_returns)(fn)

        return fn.remote(*args, **kwargs)

    def get(self, futures: list[Any]) -> list[Any]:
        """Fetch results from Ray ObjectRefs."""
        return self.ray.get(futures)

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
