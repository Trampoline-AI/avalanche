"""
Stream provider for incremental data processing.

Implements the ParameterProvider abstract base class for dependency injection of streaming data.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Generator, Generic, TypeVar

import polars as pl

from avalanche.types import ParamContext, ParameterProvider
from runtime._async import resolve_awaitable

if TYPE_CHECKING:
    from pyiceberg.table import Table

    from avalanche.iceberg import IcebergTable
    from avalanche.progress import ProgressStore
    from avalanche.types import AppendResult

T = TypeVar("T")


class Stream(ParameterProvider, Generic[T]):
    """
    Stream provider marker for incremental data processing.

    Stream() is a provider marker (like FastAPI's Depends) that tells the
    framework to inject incremental data with automatic progress tracking.

    The framework:
    - Claims snapshots for processing (with leases)
    - Reads data for that snapshot
    - Injects the DataFrame into your function
    - Marks snapshot as done/failed
    - Advances cursor for cleanup

    Two modes (automatically determined by checking parent results):

    The framework inspects parent node results (in `resolve` method) for AppendResult objects:
    - If found -> Passthrough mode (zero-copy)
    - If not found -> Table-backed mode (read from storage table)

    1. Passthrough (zero-copy): Triggered when parent.append() returned AppendResult
       - Data passed in-memory from upstream table.append()
       - Fast: No table read required
       - Use case: load_docs() >> process_docs() (same run, in-memory)

    2. Table-backed: Triggered when no parent returned AppendResult
       - Data read from the storage table
       - Resilient: Survives crashes, retries, separate workflow runs
       - Use case: Retry failed snapshots, or separate consumer workflow

    Example:
        @ava.step
        def chunk_docs(
            docs: pl.DataFrame = ava.Stream(ns.documents, key="docs_to_chunks"),
            *,
            chunks=ns.chunks,
        ):
            # 'docs' is a DataFrame for one snapshot
            # Framework handles claiming, marking done/failed, cursor advancement
            result = to_chunks(docs)
            chunks.append(result.to_arrow())
            return result

    Attributes:
        table: Table to stream from
        key: Unique key for this stream (e.g., "docs_to_chunks")
    """

    consumes_upstream = True

    table: "IcebergTable | Table"
    """Table to stream incremental data from."""

    key: str
    """Unique key for this stream (enables multiple streams per table)."""

    def __init__(self, table: "IcebergTable | Table", *, key: str):
        """
        Initialize a stream provider marker.

        Args:
            table: storage table to stream from
            key: Unique key for this stream (required, e.g., "docs_to_chunks")

        Example:
            Stream(ns.documents, key="docs_to_chunks")
        """
        self.table = table
        self.key = key

    def __repr__(self) -> str:
        return f"Stream(table={self.table}, key={self.key!r})"

    # ParameterProvider protocol implementation (class methods for registry use)
    @classmethod
    def can_resolve(cls, param_value: Any) -> bool:
        """Check if this provider can handle the parameter."""
        return isinstance(param_value, Stream)

    @classmethod
    def resolve(cls, param_value: Any, param_context: ParamContext) -> Any:
        """
        Resolve Stream to prepare for context manager wrapping.

        Uses zero-copy matching: matches parameter to parent result by position,
        following the same logic as implicit data passing.

        Returns tuple of (stream, upstream_data) for wrapper to use.

        Args:
            param_value: Stream instance to resolve
            param_context: ParamContext with parent_results, executor, etc.
        """
        from avalanche.types import AppendResult

        stream = param_value

        matching_result = param_context.get_matching_result()
        upstream_data = matching_result if isinstance(matching_result, AppendResult) else None

        return (stream, upstream_data)

    @classmethod
    def create_wrapper(
        cls, param_value: Any, original_fn: Callable, resolved_params: dict[str, Any]
    ) -> Callable | None:
        """
        Create wrapper that manages Stream context managers.

        The wrapper:
        1. Enters consume_stream context for each Stream parameter
        2. Collects DataFrames from streams
        3. Calls original function with resolved DataFrames
        4. Properly handles exceptions and cleanup
        """

        def stream_wrapper(*args, **kwargs):
            """Wrapper that resolves Stream dependencies within context managers."""
            # Build context managers for all stream parameters
            context_managers = []
            for param_name, (stream, upstream_data) in resolved_params.items():
                cm = consume_stream(stream.table, stream.key, upstream_data=upstream_data)
                context_managers.append((param_name, cm))

            # Enter contexts and collect DataFrames
            entered_contexts = []
            try:
                resolved_streams = {}
                for param_name, cm in context_managers:
                    df = cm.__enter__()
                    entered_contexts.append(cm)
                    resolved_streams[param_name] = df

                # Update kwargs with resolved streams
                kwargs.update(resolved_streams)

                # Call original function
                result = resolve_awaitable(original_fn(*args, **kwargs))

            except Exception:
                # Exit contexts with exception info
                import sys

                exc_info = sys.exc_info()
                for cm in entered_contexts:
                    try:
                        cm.__exit__(*exc_info)
                    except Exception:
                        pass
                raise

            else:
                # Exit contexts normally (only runs if NO exception was raised)
                for cm in entered_contexts:
                    try:
                        cm.__exit__(None, None, None)
                    except Exception:
                        pass
                return result

        return stream_wrapper


@contextmanager
def consume_stream(
    table: "IcebergTable | Table",
    key: str,
    *,
    progress_store: "ProgressStore | None" = None,
    upstream_data: "pl.DataFrame | AppendResult | None" = None,
) -> Generator[pl.DataFrame, None, None]:
    """
    Context manager for consuming a stream with automatic progress tracking.

    Lifecycle:
    1. Claim: Atomically claim a snapshot for processing
    2. Read: Load data (from upstream or table)
    3. Yield: Provide DataFrame to caller
    4. Complete: Mark done and advance cursor (or mark failed on exception)

    Args:
        table: Table to stream from
        key: Stream key for progress tracking
        progress_store: ProgressStore instance (created if None)
        upstream_data: Data from upstream (zero-copy mode), or None (table-backed)

    Yields:
        DataFrame for the claimed snapshot

    Example (manual usage):
        with consume_stream(table, key="docs_to_chunks") as df:
            result = process(df)
            output_table.append(result.to_arrow())

    Note:
        Normally you don't call this directly - the framework calls it when
        resolving Stream dependencies.
    """
    from avalanche.progress import ProgressStore

    # Create progress store if not provided
    if progress_store is None:
        progress_store = ProgressStore(table, key=key)

    # Determine mode based on upstream_data:
    # - If upstream_data provided: Passthrough mode (parent returned AppendResult)
    # - If upstream_data is None: Table-backed mode (read from storage table)
    if upstream_data is not None:
        # Passthrough mode: data passed from upstream (zero-copy)
        df = upstream_data.to_polars()
        snapshot_id = upstream_data.snapshot_id

        # Claim the specific snapshot to mark it as started and take a lease
        # This ensures proper tracking even in zero-copy mode
        # Raises RuntimeError if snapshot can't be claimed
        progress_store.claim(snapshot_id)

        try:
            yield df
            progress_store.mark_done(snapshot_id)
            progress_store.advance_cursor()
        except Exception as e:
            progress_store.mark_failed(snapshot_id, error=str(e))
            raise

    else:
        # Table-backed mode: read from storage table
        snapshot_id = progress_store.claim_next_pending()
        if snapshot_id is None:
            # No pending snapshots, yield empty DataFrame
            yield pl.DataFrame()
            return

        try:
            # Get the snapshot's parent to read ONLY this snapshot's data
            # This ensures atomic per-snapshot processing
            snapshot = table.snapshot_by_id(snapshot_id)
            if snapshot is None:
                raise RuntimeError(f"Claimed snapshot {snapshot_id} not found in table")
            parent_id = snapshot.parent_snapshot_id

            # Read only files added in this specific snapshot
            scan = table.append_scan(
                start_snapshot_id=parent_id,
                snapshot_id=snapshot_id,
            )

            # Collect all batches into one DataFrame
            batches = []
            for arrow_batch in scan.to_arrow_batch_reader():
                batches.append(pl.from_arrow(arrow_batch))

            if batches:
                df = pl.concat(batches)
            else:
                # Empty snapshot (e.g., schema-only change)
                df = pl.DataFrame()

            yield df
            progress_store.mark_done(snapshot_id)
            progress_store.advance_cursor()

        except Exception as e:
            progress_store.mark_failed(snapshot_id, error=str(e))
            raise
