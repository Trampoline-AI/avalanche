"""
Data types for Avalanche streaming and execution.

Defines core types used throughout the framework:
- AppendResult: Return type for table.append() operations
- SnapshotState: State tracking for per-snapshot processing
- ParameterProvider: Protocol for dependency injection
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, ClassVar, Union

import polars as pl
import pyarrow as pa


@dataclass
class AppendResult:
    """
    Result of an append operation to an Iceberg table.

    Contains both the appended data (for zero-copy passing) and the
    snapshot_id (for progress tracking).

    This enables the framework to:
    - Pass data in-memory to downstream tasks (zero-copy)
    - Track snapshot_id for progress marking
    - Support both passthrough and table-backed modes

    Attributes:
        data: The appended data (Polars DataFrame or PyArrow Table)
        snapshot_id: The snapshot ID created by this append

    Example:
        @ava.source
        def load_docs(*, documents=ns.documents):
            docs = fetch_from_s3()
            result = documents.append(docs.to_arrow())
            return result  # AppendResult(data=..., snapshot_id=...)
    """

    data: Union[pl.DataFrame, pa.Table]
    snapshot_id: int

    def to_polars(self) -> pl.DataFrame:
        """Convert data to Polars DataFrame if needed."""
        if isinstance(self.data, pl.DataFrame):
            return self.data
        return pl.from_arrow(self.data)

    def to_arrow(self) -> pa.Table:
        """Convert data to PyArrow Table if needed."""
        if isinstance(self.data, pa.Table):
            return self.data
        return self.data.to_arrow()


class SnapshotState(str, Enum):
    """
    State of a snapshot in the streaming progress tracker.

    States:
    - pending: Not yet claimed by any worker
    - started: Claimed by a worker, processing in progress
    - done: Successfully processed
    - failed: Processing failed
    - quarantined: Failed too many times, needs manual intervention
    """

    PENDING = "pending"
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"


@dataclass
class SnapshotMetadata:
    """
    Metadata for tracking snapshot processing state.

    Stored in Iceberg table properties as JSON:
    `avalanche.snapshot.<key>.<snapshot_id>`

    Attributes:
        state: Current state of this snapshot
        lease_expires_at: Unix timestamp when lease expires (None if not leased)
        attempt: Number of processing attempts (for retry/quarantine logic)
        worker_id: ID of worker currently processing (or last worker if done/failed)
        last_error: Error message from last failure (None if not failed)
    """

    state: SnapshotState
    lease_expires_at: int | None = None
    attempt: int = 0
    worker_id: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "state": self.state.value,
            "lease_expires_at": self.lease_expires_at,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotMetadata":
        """Create from dictionary (JSON deserialization)."""
        return cls(
            state=SnapshotState(data["state"]),
            lease_expires_at=data.get("lease_expires_at"),
            attempt=data.get("attempt", 0),
            worker_id=data.get("worker_id"),
            last_error=data.get("last_error"),
        )


class ParamContext:
    """
    Parameter-level context passed to providers during resolution.

    Each parameter gets its own context instance with information about:
    - The parameter being resolved (name, position)
    - The node being executed (name, function)
    - Parent results and nodes (for zero-copy matching)
    - Runtime environment (executor, worker, etc.)

    Includes helper methods for common operations like normalizing parent results
    and matching parameters to parent outputs by position (zero-copy matching).
    """

    def __init__(
        self,
        parent_results: list[Any],
        param_position: int,
        node_name: str,
        execution_id: str | None = None,
        executor_type: str = "local",
    ):
        """
        Initialize execution context for parameter resolution.

        Args:
            parent_results: Raw results from parent nodes (may be tuples/lists for multi-return)
            param_position: Position of the parameter in function signature (0-indexed)
            node_name: Name of the current node function
            execution_id: Optional unique ID for this workflow execution run
            executor_type: Type of executor ("local" or "ray") for worker_id resolution
        """
        self.parent_results = parent_results
        self.param_position = param_position
        self.node_name = node_name
        self.execution_id = execution_id
        self.executor_type = executor_type

    @property
    def upstream_results(self) -> list[Any]:
        """
        All parent results flattened into a single list.

        This follows the same logic as implicit data passing: each parent's
        results are normalized (based on num_returns) and concatenated.

        Returns:
            Flattened list of all parent result items in order

        Example:
            parent_a returns (x, y) with num_returns=2
            parent_b returns z with num_returns=1
            -> [x, y, z]
        """
        flattened = []
        for result in self.parent_results:
            if result is not None:
                # Normalize: handle multi-return (tuple/list) vs single-return
                items = list(result) if isinstance(result, (tuple, list)) else [result]
                flattened.extend(items)
        return flattened

    def get_matching_result(self) -> Any | None:
        """
        Get the upstream result that matches this parameter by position.

        Uses zero-copy matching logic: parameters are matched to parent results
        by position in the flattened results list.

        Returns:
            The upstream result at this parameter's position, or None if out of bounds

        Example:
            Upstream outputs: [x, y, z]
            Parameter position: 1
            -> Returns y
        """
        if self.param_position < len(self.upstream_results):
            return self.upstream_results[self.param_position]
        return None


class ParameterProvider(ABC):
    """
    Abstract base class for dependency injection in DAG execution.

    Providers must inherit from this class and implement all abstract methods.
    This enables extensible parameter injection (Stream, Logger, Config, etc.)
    without modifying DAG execution logic.

    Example:
        class Stream(ParameterProvider):
            @classmethod
            def can_resolve(cls, param_value: Any) -> bool:
                return isinstance(param_value, Stream)

            @classmethod
            def resolve(cls, param_value: Any, param_context: ParamContext) -> Any:
                # Find upstream data and prepare stream
                return resolved_value

            @classmethod
            def create_wrapper(cls, param_value, original_fn, resolved_params):
                # Override for special behavior (context managers, transactions, etc.)
                # Default injects resolved_params into kwargs
                return custom_wrapper_fn
    """

    consumes_upstream: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def can_resolve(cls, param_value: Any) -> bool:
        """Check if this provider can handle the given parameter value."""
        ...

    @classmethod
    @abstractmethod
    def resolve(cls, param_value: Any, param_context: ParamContext) -> Any:
        """
        Resolve the parameter to its actual runtime value for injection.

        Args:
            param_value: The parameter value from node invocation
            param_context: Parameter-level context with parent results, node info, etc.
                          Use param_context for common operations:
                          - upstream_results: All upstream results as flat list (property)
                          - get_matching_result(): Match by position (zero-copy)

        Returns:
            Resolved value to inject into the function
        """
        ...

    @classmethod
    def create_wrapper(
        cls, param_value: Any, original_fn: Callable, resolved_params: dict[str, Any]
    ) -> Callable | None:
        """
        Create a wrapper function if special behavior is needed.

        Default returns None - DAG will inject resolved_params into kwargs.
        Override for special behavior (context managers, transactions, etc.).

        Args:
            param_value: The original parameter value
            original_fn: The original function to wrap
            resolved_params: Dict of param_name -> resolved_value for this provider

        Returns:
            Wrapped function for special behavior, or None for default injection
        """
        return None
