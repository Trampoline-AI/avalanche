"""
Stream provider for incremental data processing.

Implements the ParameterProvider abstract base class for dependency injection of streaming data.
"""

import hashlib
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Generator, Generic, TypeVar

import polars as pl
from pyiceberg.exceptions import CommitFailedException

from avalanche.types import ParamContext, ParameterProvider
from runtime._async import resolve_awaitable

if TYPE_CHECKING:
    from pyiceberg.table import Table

    from avalanche.iceberg import IcebergTable
    from avalanche.progress import ProgressStore
    from avalanche.runtime import Rerun, RunContext
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
       - Resilient: Survives crashes, repeated attempts, separate workflow runs
       - Use case: Recover failed snapshots, or separate consumer workflow

    3. Rerun: Triggered by Workflow.run(rerun=...)
       - Reads rows for the source run via row-lineage columns
       - Bypasses ProgressStore entirely
       - Use case: re-execute a past run from selected node slugs

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

        Returns tuple of (stream, upstream_data, source_node_slugs) for wrapper
        to use.

        Args:
            param_value: Stream instance to resolve
            param_context: ParamContext with parent_results, executor, etc.
        """
        from avalanche.types import AppendResult, unwrap_lineaged

        stream = param_value

        matching_result = param_context.materialize(param_context.get_matching_result())
        matching_result = unwrap_lineaged(matching_result)
        upstream_data = matching_result if isinstance(matching_result, AppendResult) else None
        matching_slug = param_context.get_matching_node_slug()
        source_node_slugs = (
            (matching_slug,)
            if matching_slug
            else tuple(param_context.upstream_node_slugs)
        )

        return (stream, upstream_data, source_node_slugs)

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
            for param_name, resolved in resolved_params.items():
                stream, upstream_data, source_node_slugs = resolved
                cm = consume_stream(
                    stream.table,
                    stream.key,
                    upstream_data=upstream_data,
                    source_node_slugs=source_node_slugs,
                )
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
                # Exit contexts normally (only runs if NO exception was raised
                # by the user function). Unlike the failure path, exceptions
                # from __exit__ here are real (e.g. a failed durable rerun-edge
                # persistence) and must not be silently swallowed. Attempt to
                # close every context, then re-raise the first exit exception.
                exit_error: BaseException | None = None
                for cm in entered_contexts:
                    try:
                        cm.__exit__(None, None, None)
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        if exit_error is None:
                            exit_error = exc
                if exit_error is not None:
                    raise exit_error
                return result

        return stream_wrapper


@contextmanager
def consume_stream(
    table: "IcebergTable | Table",
    key: str,
    *,
    progress_store: "ProgressStore | None" = None,
    upstream_data: "pl.DataFrame | AppendResult | None" = None,
    rerun: "Rerun | None" = None,
    source_node_slugs: tuple[str, ...] = (),
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
        rerun: Optional explicit rerun spec for manual usage. Workflow runs pass
            this via RunContext.

    Yields:
        DataFrame for the claimed snapshot or rerun source rows

    Example (manual usage):
        with consume_stream(table, key="docs_to_chunks") as df:
            result = process(df)
            output_table.append(result.to_arrow())

    Note:
        Normally you don't call this directly - the framework calls it when
        resolving Stream dependencies.
    """
    from avalanche.progress import ProgressStore
    from avalanche.runtime import get_current_run_context

    context = get_current_run_context()
    active_rerun = rerun or (context.rerun if context is not None else None)

    if active_rerun is not None:
        if upstream_data is not None:
            df = _upstream_to_polars(upstream_data)
        else:
            df = _read_rerun_rows(
                table,
                active_rerun.run_id,
                node_slugs=source_node_slugs,
            )
        _merge_input_lineage(context, df)
        yield df
        # Persist the rerun-of edge only after the consumer generator resumes
        # (i.e. the node body ran without raising) so a failed node does not
        # leave a durable ancestry edge for a run that produced nothing.
        # Mirrors the progress store's success-after-processing model.
        if context is not None:
            _record_rerun_edge(
                table,
                run_id=context.run_id,
                source_run_id=active_rerun.run_id,
            )
        return

    # Create progress store if not provided
    if progress_store is None:
        progress_store = ProgressStore(table, key=key)

    # Determine mode based on upstream_data:
    # - If upstream_data provided: Passthrough mode (parent returned AppendResult)
    # - If upstream_data is None: Table-backed mode (read from storage table)
    if upstream_data is not None:
        # Passthrough mode: data passed from upstream (zero-copy)
        df = _upstream_to_polars(upstream_data)
        _merge_input_lineage(context, df)
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
        # Table-backed mode: read from storage table.
        #
        # NOTE: One snapshot is claimed per consume by design. This preserves
        # per-snapshot failure isolation: if the consumer raises, only this
        # snapshot is marked failed/retryable and no later snapshot's data is
        # leaked into the failed batch (see
        # test_failed_table_backed_snapshot_retries_without_data_leakage).
        # Draining the whole backlog in one DataFrame is a separate opt-in
        # batching design (tracked as a scan-mode follow-up), not a safe change
        # to the default contract.
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

            _merge_input_lineage(context, df)

            yield df
            progress_store.mark_done(snapshot_id)
            progress_store.advance_cursor()

        except Exception as e:
            progress_store.mark_failed(snapshot_id, error=str(e))
            raise


def _upstream_to_polars(upstream_data: Any) -> pl.DataFrame:
    if isinstance(upstream_data, pl.DataFrame):
        return upstream_data
    return upstream_data.to_polars()


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _lineage_scan_filter(run_id: str, node_slugs: tuple[str, ...] = ()) -> str:
    filter_expr = f"_ava_run_id = {_sql_string(run_id)}"
    unique_slugs = tuple(dict.fromkeys(node_slugs))
    if not unique_slugs:
        return filter_expr
    slug_expr = " OR ".join(
        f"_ava_node_slug = {_sql_string(slug)}" for slug in unique_slugs
    )
    return f"{filter_expr} AND ({slug_expr})"


def _scan_run_rows(
    table: Any,
    run_id: str,
    *,
    node_slugs: tuple[str, ...] = (),
) -> pl.DataFrame:
    return table.scan(filter=_lineage_scan_filter(run_id, node_slugs)).to_polars()


def _rerun_edge_property_key(run_id: str) -> str:
    # Run IDs are user-provided strings that may contain characters awkward for
    # property keys; hash to a stable, safe key.
    digest = hashlib.sha256(run_id.encode()).hexdigest()
    return f"avalanche.rerun.edge.{digest}"


def _record_rerun_edge(table: Any, *, run_id: str, source_run_id: str) -> None:
    """Persist a durable rerun-of edge on a consumed table.

    A lazy rerun consumes an input table without necessarily writing rows to
    it, so the parent-run link cannot be recovered from row lineage alone. We
    store it as a table property keyed by the current run so a later
    rerun-of-rerun can resolve the ancestry chain even when the intermediate
    run wrote zero payload rows here.
    """
    if run_id == source_run_id:
        return
    key = _rerun_edge_property_key(run_id)
    last_error: Exception | None = None
    for _ in range(3):
        table.refresh()
        if table.properties.get(key) == source_run_id:
            return
        try:
            with table.transaction() as tx:
                tx.set_properties(**{key: source_run_id})
            return
        except CommitFailedException as exc:
            last_error = exc

    raise RuntimeError(
        f"Failed to record rerun edge for run {run_id!r} -> {source_run_id!r}"
    ) from last_error


def _resolve_rerun_parent(table: Any, run_id: str, df: pl.DataFrame) -> str | None:
    """Resolve the parent run for a rerun run.

    Resolution order:
    1. durable table-property edge (works for sparse lazy reruns that wrote no
       rows here);
    2. `_ava_rerun_of` from the slug-filtered payload frame;
    3. `_ava_rerun_of` from an unfiltered scan of all rows for the run (covers
       runs recorded before the edge property existed where the requested slug
       happened to write no rows).
    """
    table.refresh()
    edge = table.properties.get(_rerun_edge_property_key(run_id))
    if edge:
        return edge

    parent = _unique_rerun_parent(df, run_id)
    if parent is not None:
        return parent

    return _unique_rerun_parent(_scan_run_rows(table, run_id), run_id)


def _unique_rerun_parent(df: pl.DataFrame, run_id: str) -> str | None:
    if "_ava_rerun_of" not in df.columns:
        return None
    parents = [
        parent
        for parent in df["_ava_rerun_of"].drop_nulls().unique().to_list()
        if parent
    ]
    if not parents:
        return None
    if len(parents) > 1:
        raise ValueError(
            f"Ambiguous rerun ancestry for run {run_id!r}: found multiple parent "
            f"runs {sorted(parents)!r} in row lineage. Cannot resolve a single "
            "rerun-of edge."
        )
    return parents[0]


def _read_rerun_rows(
    table: Any,
    run_id: str,
    *,
    node_slugs: tuple[str, ...] = (),
) -> pl.DataFrame:
    if not getattr(table, "row_lineage", False):
        raise ValueError("Rerun streams require tables created with row_lineage=True")

    frames: list[pl.DataFrame] = []
    current_run_id: str | None = run_id
    seen: set[str] = set()
    empty_result: pl.DataFrame | None = None

    while current_run_id is not None and current_run_id not in seen:
        seen.add(current_run_id)
        df = _scan_run_rows(table, current_run_id, node_slugs=node_slugs)
        if empty_result is None:
            empty_result = df
        if not df.is_empty():
            frames.append(df)

        # Resolve the parent run independently of the slug-filtered payload
        # scan: a sparse lazy rerun writes no rows here, so the edge lives in a
        # durable table property recorded at consume time.
        current_run_id = _resolve_rerun_parent(table, current_run_id, df)

    if not frames:
        return empty_result if empty_result is not None else pl.DataFrame()

    return _overlay_rerun_frames(frames)


def _overlay_rerun_frames(frames: list[pl.DataFrame]) -> pl.DataFrame:
    selected: list[pl.DataFrame] = []
    seen_slugs: set[str] = set()

    for df in frames:
        if "_ava_node_slug" not in df.columns:
            selected.append(df)
            continue
        for slug in df["_ava_node_slug"].unique().to_list():
            key = slug or ""
            if key in seen_slugs:
                continue
            seen_slugs.add(key)
            if slug is None:
                selected.append(df.filter(pl.col("_ava_node_slug").is_null()))
            else:
                selected.append(df.filter(pl.col("_ava_node_slug") == slug))

    if not selected:
        return frames[0].head(0)
    return pl.concat(selected, how="vertical")


def _merge_input_lineage(context: "RunContext | None", df: pl.DataFrame) -> None:
    if context is None or df.is_empty():
        return

    lineage = dict(context.lineage_vector)
    if "_ava_lineage_vector" in df.columns:
        for raw_value in df["_ava_lineage_vector"].drop_nulls().unique().to_list():
            try:
                parsed = json.loads(raw_value)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                lineage.update({str(k): str(v) for k, v in parsed.items()})

    if {"_ava_node_slug", "_ava_run_id"}.issubset(df.columns):
        pairs = df.select(["_ava_node_slug", "_ava_run_id"]).drop_nulls().unique()
        for row in pairs.to_dicts():
            lineage[str(row["_ava_node_slug"])] = str(row["_ava_run_id"])

    context.lineage_vector = lineage
