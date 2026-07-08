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
        data: The appended data (Polars DataFrame, PyArrow Table, or RecordBatch)
        snapshot_id: The snapshot ID created by this append

    Example:
        @ava.source
        def load_docs(*, documents=ns.documents):
            docs = fetch_from_s3()
            result = documents.append(docs.to_arrow())
            return result  # AppendResult(data=..., snapshot_id=...)
    """

    data: Union[pl.DataFrame, pa.Table, pa.RecordBatch]
    snapshot_id: int
    table_identity: str | None = None

    def to_polars(self) -> pl.DataFrame:
        """Convert data to Polars DataFrame if needed."""
        if isinstance(self.data, pl.DataFrame):
            return self.data
        result = pl.from_arrow(self.to_arrow())
        assert isinstance(result, pl.DataFrame)
        return result

    def to_arrow(self) -> pa.Table:
        """Convert data to PyArrow Table if needed."""
        if isinstance(self.data, pa.Table):
            return self.data
        if isinstance(self.data, pa.RecordBatch):
            return pa.Table.from_batches([self.data])
        return self.data.to_arrow()

    def to_dicts(self) -> list[dict[str, Any]]:
        """Return appended rows as Python dictionaries."""
        return self.to_polars().to_dicts()


@dataclass
class AppendResultHandle:
    """Control-plane handle for an ``AppendResult`` whose data lives off-driver.

    Under a distributed executor (Ray), a task that returns an ``AppendResult``
    is normalized into this small handle: the frame is placed in the object
    store and only the ``data_ref`` plus tiny metadata travel as the task
    payload. The driver can inspect the handle (snapshot id, table identity,
    the data ref) to detect Stream passthrough without materializing the frame.
    The consumer worker dereferences ``data_ref`` when it actually needs rows.

    Internal transport type: always materialized back into a public
    ``AppendResult`` before reaching user functions, hooks, or the final
    workflow return.
    """

    data_ref: Any
    snapshot_id: int
    table_identity: str | None = None


@dataclass
class DeferredStreamUpstream:
    """Worker-side Stream parent dependency (distributed executors only).

    When a ``Stream`` consumer runs under Ray, the driver must NOT fetch the
    producer's result just to detect passthrough mode. Instead the parent ref
    is injected as an explicit top-level hidden task kwarg (named ``parent_kwarg``)
    so Ray tracks it as a real scheduling dependency, and this carrier travels in
    the Stream wrapper closure with only the kwarg name plus tiny metadata.

    Inside the consumer worker the wrapper pops ``parent_kwarg`` from kwargs —
    Ray has already auto-dereferenced the producer result ref to its value,
    which is a small ``AppendResultHandle`` (control metadata). The wrapper then
    dereferences the handle's ``data_ref`` to the actual frame worker-side, so
    the appended data never crosses the driver AND the consumer is never
    scheduled before the producer completes.

    Ray-serializable: carries only a kwarg name plus tiny metadata, never a ref
    or executor object.
    """

    parent_kwarg: str
    table_identity: str | None = None
    # Driver-planning only: the producer payload ref. The DAG submit path lifts
    # this into a top-level hidden task kwarg (so Ray tracks it as a real
    # dependency) and then strips it to None before the wrapper closure is
    # serialized, so a parent ref never travels inside the closure.
    ref: Any = None


@dataclass
class LineagedResult:
    """A node return value carrying the lineage vector of its producers.

    Reruns must record, on each produced row, which producer versions were
    consumed (`_ava_lineage_vector`). Stream inputs merge that lineage inside
    the worker/runtime context during `consume_stream`, but a node that
    consumes an upstream result through an ordinary Python argument would
    otherwise lose it once the value crosses the executor boundary.

    The framework wraps node results in `LineagedResult` so downstream nodes can
    merge the producer lineage regardless of executor. It is an internal
    transport type: it is always unwrapped before reaching user functions, hooks
    tests, or the final workflow return.
    """

    value: Any
    lineage_vector: dict[str, str]


def unwrap_lineaged(value: Any) -> Any:
    """Return the underlying value, stripping a LineagedResult envelope."""
    return value.value if isinstance(value, LineagedResult) else value


def materialize_append_handles(value: Any, get_data: "Callable[[Any], Any]") -> Any:
    """Convert internal ``AppendResultHandle``s back into public ``AppendResult``.

    ``get_data`` dereferences a data ref to the concrete frame. Two call sites:
    - worker-side (``ray.get``) before user code / Stream consumption runs, so
      user functions never see the internal handle;
    - driver-side (``executor.get``) only for explicit workflow returns and
      ``unwrap_result`` hooks, where materialization is intentional.

    Recurses through ``LineagedResult`` envelopes and tuple/list/dict
    containers, preserving shape and lineage.
    """
    if isinstance(value, LineagedResult):
        return LineagedResult(
            materialize_append_handles(value.value, get_data),
            dict(value.lineage_vector),
        )
    if isinstance(value, AppendResultHandle):
        return AppendResult(
            data=get_data(value.data_ref),
            snapshot_id=value.snapshot_id,
            table_identity=value.table_identity,
        )
    if isinstance(value, tuple):
        return tuple(materialize_append_handles(v, get_data) for v in value)
    if isinstance(value, list):
        return [materialize_append_handles(v, get_data) for v in value]
    if isinstance(value, dict):
        return {k: materialize_append_handles(v, get_data) for k, v in value.items()}
    return value


def lineage_of(value: Any) -> dict[str, str]:
    """Return the lineage vector carried by a value, or empty if none."""
    return dict(value.lineage_vector) if isinstance(value, LineagedResult) else {}


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
        node_slug: str | None = None,
        upstream_node_slugs: list[str] | None = None,
        run_id: str | None = None,
        rerun: Any = None,
        preserve_missing_results: bool = False,
        executor_type: str = "local",
        executor: Any = None,
    ):
        """
        Initialize execution context for parameter resolution.

        Args:
            parent_results: Raw results from parent nodes (may be tuples/lists for multi-return)
            param_position: Position of the parameter in function signature (0-indexed)
            node_name: Name of the current node function
            run_id: Optional unique ID for this workflow execution run
            executor_type: Type of executor ("local" or "ray") for worker_id resolution
            executor: The active executor, used to materialize distributed refs
                (e.g. Ray ObjectRef) during provider resolution
        """
        self.parent_results = parent_results
        self.param_position = param_position
        self.node_name = node_name
        self.node_slug = node_slug or node_name
        self.upstream_node_slugs = upstream_node_slugs or []
        self.run_id = run_id
        self.rerun = rerun
        self.preserve_missing_results = preserve_missing_results
        self.executor_type = executor_type
        self.executor = executor

    def materialize(self, value: Any) -> Any:
        """Fetch distributed executor refs to concrete values if needed.

        LocalExecutor values are already materialized. Ray ObjectRefs are
        fetched via ``executor.get``. Recurses through tuple/list/dict,
        preserving container shape.

        NOTE: the Stream provider no longer calls this on Ray ObjectRefs — it
        defers parent resolution into the consumer worker via
        ``DeferredStreamUpstream`` so the appended frame is never pulled to the
        driver. This helper remains for providers/paths that legitimately need
        a concrete value at resolution time; avoid using it on large Ray
        payloads on the driver.
        """
        executor = self.executor
        if executor is None:
            return value
        ray = getattr(executor, "ray", None)
        object_ref_type = getattr(ray, "ObjectRef", None)
        if object_ref_type is not None and isinstance(value, object_ref_type):
            return executor.get([value])[0]
        if isinstance(value, tuple):
            return tuple(self.materialize(item) for item in value)
        if isinstance(value, list):
            return [self.materialize(item) for item in value]
        if isinstance(value, dict):
            return {k: self.materialize(v) for k, v in value.items()}
        return value

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
            elif self.preserve_missing_results:
                flattened.append(None)
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

    def get_matching_node_slug(self) -> str | None:
        """Get the upstream node slug that matches this parameter by position."""
        if self.param_position < len(self.upstream_node_slugs):
            return self.upstream_node_slugs[self.param_position]
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
