"""
Stream provider for incremental data processing.

Implements the ParameterProvider abstract base class for dependency injection of streaming data.
"""

import hashlib
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Generator, Generic, Literal, TypeVar

import polars as pl
from pydantic import BaseModel
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
ModelT = TypeVar("ModelT", bound=BaseModel)

StreamMode = Literal["run_scoped", "append_scan"]
ModelStreamCardinality = Literal["one", "one_or_none", "all"]


def _table_identity(table: Any) -> str | None:
    """Best-effort stable identity for a storage table (Iceberg id / location)."""
    return getattr(table, "identifier", None) or getattr(table, "location", None) or None


def _table_label(table: Any) -> str:
    """Human-readable table identity for model-stream errors."""
    identity = _table_identity(table)
    if identity:
        return str(identity)
    table_name = getattr(table, "_table_name", None)
    if table_name:
        return str(table_name)
    return type(table).__name__


def _is_executor_ref(value: Any, executor: Any) -> bool:
    """True when ``value`` is a concrete distributed ref (Ray ObjectRef)."""
    if executor is None:
        return False
    ray = getattr(executor, "ray", None)
    object_ref_type = getattr(ray, "ObjectRef", None)
    return object_ref_type is not None and isinstance(value, object_ref_type)


def _ray_get(value: Any) -> Any:
    """Dereference a Ray ObjectRef if that is what ``value`` is.

    Called only inside a Ray worker (from ``stream_wrapper``), never on the
    driver — so the appended frame is materialized worker-side. Tolerant of
    already-materialized values so nested-ref serialization quirks and tests
    that pass concrete handles both work.
    """
    import ray

    object_ref_type = getattr(ray, "ObjectRef", None)
    if object_ref_type is not None and isinstance(value, object_ref_type):
        return ray.get(value)
    return value


_MISSING_PARENT = object()  # sentinel: hidden parent kwarg not supplied


def _resolve_deferred_stream_upstream(
    upstream_data: Any, parent_value: Any = _MISSING_PARENT
) -> Any:
    """Resolve a ``DeferredStreamUpstream`` to an ``AppendResult`` worker-side.

    Runs inside the consumer task (via ``stream_wrapper``). ``parent_value`` is
    the producer result that Ray already auto-dereferenced from the hidden
    top-level kwarg — a small ``AppendResultHandle`` (control metadata) under
    the control/data split. This converts the handle into a public
    ``AppendResult`` (fetching the frame's ``data_ref`` only here, worker-side)
    and validates table identity. Non-passthrough parents fall back to
    table-backed mode (None).

    A producer may legitimately return ``None`` (→ table-backed fallback), so a
    dedicated ``_MISSING_PARENT`` sentinel — not ``None`` — signals a missing
    hidden kwarg.
    """
    from avalanche.types import (
        AppendResult,
        AppendResultHandle,
        DeferredStreamUpstream,
        LineagedResult,
        materialize_append_handles,
        unwrap_lineaged,
    )

    if not isinstance(upstream_data, DeferredStreamUpstream):
        return upstream_data

    if parent_value is _MISSING_PARENT:
        raise RuntimeError(
            "Deferred Stream upstream is missing its hidden parent kwarg; the "
            "DAG submit path must inject it as a top-level task argument"
        )

    # Inspect CONTROL metadata before touching the data plane. parent_value is
    # already auto-dereferenced by Ray; the frame still lives behind
    # AppendResultHandle.data_ref. Validate table identity first so a mismatched
    # parent never triggers a data-plane fetch.
    control = parent_value
    if isinstance(control, LineagedResult):
        control = control.value

    expected = upstream_data.table_identity

    if isinstance(control, AppendResultHandle):
        actual = control.table_identity
        if expected is not None and actual is not None and expected != actual:
            # Different table: not this Stream's passthrough. No frame fetched.
            return None
        # Passthrough confirmed on metadata alone; now fetch the frame
        # worker-side (materialize_append_handles dereferences data_ref).
        parent = materialize_append_handles(parent_value, _ray_get)
        parent = unwrap_lineaged(parent)
        return parent if isinstance(parent, AppendResult) else None

    if isinstance(control, AppendResult):
        # Already-materialized AppendResult (no handle indirection).
        actual = control.table_identity
        if expected is not None and actual is not None and expected != actual:
            return None
        return control

    # Parent was not an AppendResult/handle (plain frame, None, unrelated
    # value): not a passthrough for this Stream — use table-backed mode.
    return None


class Stream(ParameterProvider, Generic[T]):
    """
    Stream provider marker that injects a DataFrame into a task parameter.

    Stream() is a provider marker (like FastAPI's Depends) that tells the
    framework to resolve a table read for a task parameter. It defaults to
    run-scoped reads; append-scan (backlog/cursor) is opt-in via ``mode``.

    Durable read mode (``mode``) decides how a stream reads from the table when
    the data is not already available in memory:

    1. ``run_scoped`` (default): read the rows this workflow run produced,
       matched by row-lineage columns (``_ava_run_id`` / ``_ava_node_slug``).
       - No ProgressStore, no cursor, no backlog draining.
       - ``key`` is not accepted; there is no cursor to name.
       - Use case: multi-agent / checkpoint pipelines where a table is a
         run-scoped store of results, not a queue.

    2. ``append_scan``: queue/backlog mode. Claim one pending snapshot via
       ``ProgressStore`` and read it through ``append_scan``.
       - Requires ``key`` to identify the progress cursor.
       - Resilient: survives crashes, repeated attempts, separate consumer runs.
       - Use case: incremental ingestion where each new snapshot is processed
         once and the backlog is drained over successive runs.

    Passthrough (zero-copy) is an orthogonal optimization that can short-circuit
    either mode: if a parent node returned an ``AppendResult``, its in-memory
    data is used directly and no table read happens. ProgressStore bookkeeping
    for ``append_scan`` is preserved in that case.

    Rerun overrides ``mode``: when ``Workflow.run(rerun=...)`` is active, every
    stream reads run-scoped rows for the source run via row lineage (with the
    rerun-ancestry overlay) and bypasses ProgressStore, regardless of ``mode``.

    Example:
        @ava.step
        def chunk_docs(
            docs: pl.DataFrame = ava.Stream(ns.documents),
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
        key: Unique key for the progress cursor (required for ``append_scan``).
        mode: Durable read mode, ``run_scoped`` (default) or ``append_scan``.
    """

    consumes_upstream = True

    table: "IcebergTable | Table"
    """Table to stream incremental data from."""

    key: "str | None"
    """Progress-cursor key (enables multiple streams per table). Required for
    ``append_scan``; unused for ``run_scoped``."""

    mode: StreamMode
    """Durable read mode: ``run_scoped`` (default) or ``append_scan``."""

    def __init__(
        self,
        table: "IcebergTable | Table",
        *,
        key: "str | None" = None,
        mode: StreamMode = "run_scoped",
    ):
        """
        Initialize a stream provider marker.

        Args:
            table: storage table to stream from
            key: progress-cursor key. Required when ``mode="append_scan"``;
                must be omitted for ``run_scoped`` (it has no meaning there).
            mode: durable read mode. ``run_scoped`` (default) reads the current
                run's rows via row lineage; ``append_scan`` drains pending
                snapshots through ProgressStore.

        Example:
            Stream(ns.documents)                              # run_scoped
            Stream(ns.documents, key="docs_to_chunks",
                   mode="append_scan")                        # backlog queue
        """
        if mode not in ("run_scoped", "append_scan"):
            raise ValueError(f"Stream mode must be 'run_scoped' or 'append_scan', got {mode!r}")
        if mode == "append_scan" and key is None:
            raise ValueError("append_scan streams require key=...")
        if mode == "run_scoped" and key is not None:
            raise ValueError(
                "run_scoped streams do not use key=...; omit key or set "
                "mode='append_scan' for backlog/cursor streaming"
            )
        self.table = table
        self.key = key
        self.mode = mode

    def __repr__(self) -> str:
        return f"Stream(table={self.table}, key={self.key!r}, mode={self.mode!r})"

    def _materialize(
        self, df: pl.DataFrame, source_node_slugs: tuple[str, ...]
    ) -> pl.DataFrame:
        """Project the consumed frame into the value injected into user code."""
        return df

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
        from avalanche.types import AppendResult, DeferredStreamUpstream, unwrap_lineaged

        stream = param_value

        raw_matching_result = param_context.get_matching_result()
        if _is_executor_ref(raw_matching_result, param_context.executor):
            # Distributed executor (Ray): do NOT fetch the parent to the driver
            # just to detect passthrough. Carry the parent payload ref so the
            # DAG submit path can lift it into a top-level hidden task kwarg
            # (making Ray track it as a real scheduling dependency), then defer
            # resolution into the consumer worker where the small
            # AppendResultHandle (and only then its data ref) is dereferenced.
            # The final, collision-free hidden kwarg name is stamped in the DAG
            # submit lift (where node_id + param name are available); use a
            # placeholder here.
            upstream_data: Any = DeferredStreamUpstream(
                parent_kwarg="",
                table_identity=_table_identity(stream.table),
                ref=raw_matching_result,
            )
        else:
            # Local / already-materialized value: keep the existing behavior.
            matching_result = param_context.materialize(raw_matching_result)
            matching_result = unwrap_lineaged(matching_result)
            upstream_data = (
                matching_result if isinstance(matching_result, AppendResult) else None
            )
        matching_slug = param_context.get_matching_node_slug()
        source_node_slugs = (
            (matching_slug,) if matching_slug else tuple(param_context.upstream_node_slugs)
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
            from avalanche.types import DeferredStreamUpstream

            # Build context managers for all stream parameters
            context_managers = []
            for param_name, resolved in resolved_params.items():
                stream, upstream_data, source_node_slugs = resolved
                # Worker-side boundary: if the driver deferred the parent (Ray),
                # the parent payload ref was lifted into a top-level hidden task
                # kwarg so Ray tracked it as a real dependency. Pop it (so it
                # never leaks into the user function) and resolve the nested
                # AppendResultHandle's data ref here — the appended frame is
                # materialized inside this consumer task, never on the driver.
                parent_value = _MISSING_PARENT
                if isinstance(upstream_data, DeferredStreamUpstream):
                    parent_value = kwargs.pop(upstream_data.parent_kwarg, _MISSING_PARENT)
                upstream_data = _resolve_deferred_stream_upstream(upstream_data, parent_value)
                cm = consume_stream(
                    stream.table,
                    stream.key,
                    mode=stream.mode,
                    upstream_data=upstream_data,
                    source_node_slugs=source_node_slugs,
                )
                context_managers.append((param_name, stream, source_node_slugs, cm))

            # Enter contexts and collect DataFrames
            entered_contexts = []
            try:
                resolved_streams = {}
                for param_name, stream, source_node_slugs, cm in context_managers:
                    df = cm.__enter__()
                    entered_contexts.append(cm)
                    resolved_streams[param_name] = stream._materialize(df, source_node_slugs)

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


class ModelStream(Stream[ModelT], Generic[ModelT]):
    """Stream provider that injects validated pydantic row models."""

    cardinality: ModelStreamCardinality

    def __init__(
        self,
        table: "IcebergTable | Table",
        *,
        cardinality: ModelStreamCardinality,
        key: str | None = None,
        mode: StreamMode = "run_scoped",
    ):
        if getattr(table, "row_model", None) is None:
            raise TypeError(
                "ModelStream requires a table declared with a pydantic model schema; "
                f"table={_table_label(table)!r}."
            )
        super().__init__(table, key=key, mode=mode)
        self.cardinality = cardinality

    @classmethod
    def one(
        cls,
        table: "IcebergTable | Table",
        *,
        key: str | None = None,
        mode: StreamMode = "run_scoped",
    ) -> "ModelStream[ModelT]":
        """Inject exactly one validated row model."""
        return cls(table, cardinality="one", key=key, mode=mode)

    @classmethod
    def one_or_none(
        cls,
        table: "IcebergTable | Table",
        *,
        key: str | None = None,
        mode: StreamMode = "run_scoped",
    ) -> "ModelStream[ModelT]":
        """Inject one validated row model, or None when no rows exist."""
        return cls(table, cardinality="one_or_none", key=key, mode=mode)

    @classmethod
    def all(
        cls,
        table: "IcebergTable | Table",
        *,
        key: str | None = None,
        mode: StreamMode = "run_scoped",
    ) -> "ModelStream[ModelT]":
        """Inject all validated row models in stable table order."""
        return cls(table, cardinality="all", key=key, mode=mode)

    def __repr__(self) -> str:
        return (
            f"ModelStream.{self.cardinality}(table={self.table}, key={self.key!r}, "
            f"mode={self.mode!r})"
        )

    @classmethod
    def can_resolve(cls, param_value: Any) -> bool:
        """Resolve only model streams; Stream handles dataframe streams."""
        return isinstance(param_value, cls)

    def _materialize(
        self, df: pl.DataFrame, source_node_slugs: tuple[str, ...]
    ) -> ModelT | None | list[ModelT]:
        from avalanche.model_frame import arrow_to_models
        from avalanche.runtime import get_current_run_context

        context = get_current_run_context()
        details = [f"table={_table_label(self.table)!r}"]
        if context is not None:
            details.extend(
                (
                    f"workflow={context.workflow_name!r}",
                    f"run_id={context.run_id!r}",
                )
            )
        if len(source_node_slugs) == 1:
            details.append(f"source_node={source_node_slugs[0]!r}")
        elif source_node_slugs:
            details.append(f"source_nodes={source_node_slugs!r}")
        error_context = ", ".join(details)

        row_count = df.height
        if self.cardinality == "one" and row_count != 1:
            raise ValueError(
                "ModelStream.one expected exactly one row; "
                f"got {row_count} rows ({error_context})."
            )
        if self.cardinality == "one_or_none":
            if row_count == 0:
                return None
            if row_count > 1:
                raise ValueError(
                    "ModelStream.one_or_none expected at most one row; "
                    f"got {row_count} rows ({error_context})."
                )

        try:
            models = arrow_to_models(df, self.table.row_model)
        except Exception as exc:
            raise ValueError(
                f"ModelStream failed to validate rows ({error_context}): {exc}"
            ) from exc

        if self.cardinality == "all":
            return models
        return models[0]


@contextmanager
def consume_stream(
    table: "IcebergTable | Table",
    key: "str | None" = None,
    *,
    mode: StreamMode = "run_scoped",
    progress_store: "ProgressStore | None" = None,
    upstream_data: "pl.DataFrame | AppendResult | None" = None,
    rerun: "Rerun | None" = None,
    source_node_slugs: tuple[str, ...] = (),
) -> Generator[pl.DataFrame, None, None]:
    """
    Context manager for consuming a stream with automatic progress tracking.

    Mode selection (durable read plan when data is not passed in memory):
    - Rerun active (RunContext.rerun or explicit ``rerun=``): run-scoped replay
      of the source run's rows, bypassing ProgressStore. Overrides ``mode``.
    - ``mode="run_scoped"`` (default): read the current run's rows via row
      lineage. No ProgressStore, no cursor.
    - ``mode="append_scan"``: claim one pending snapshot via ProgressStore and
      read it through ``append_scan``. Requires ``key``.

    Passthrough (``upstream_data`` set) short-circuits the table read in every
    mode; ProgressStore bookkeeping is still performed for ``append_scan``.

    Args:
        table: Table to stream from
        key: Progress-cursor key. Required for ``mode="append_scan"``.
        mode: Durable read mode, ``run_scoped`` (default) or ``append_scan``.
        progress_store: ProgressStore instance (created if None)
        upstream_data: Data from upstream (zero-copy mode), or None (table-backed)
        rerun: Optional explicit rerun spec for manual usage. Workflow runs pass
            this via RunContext.

    Yields:
        DataFrame for the claimed snapshot or rerun source rows

    Example (manual usage):
        with consume_stream(table, key="docs_to_chunks", mode="append_scan") as df:
            result = process(df)
            output_table.append(result.to_arrow())

    Note:
        Normally you don't call this directly - the framework calls it when
        resolving Stream dependencies.
    """
    from avalanche.progress import ProgressStore
    from avalanche.runtime import get_current_run_context

    if mode not in ("run_scoped", "append_scan"):
        raise ValueError(f"Stream mode must be 'run_scoped' or 'append_scan', got {mode!r}")

    context = get_current_run_context()
    active_rerun = rerun or (context.rerun if context is not None else None)

    # Rerun overrides durable mode, so mode/key compatibility is only enforced
    # for ordinary (non-rerun) execution.
    if active_rerun is None:
        if mode == "append_scan" and key is None:
            raise ValueError("append_scan streams require key=...")
        if mode == "run_scoped" and key is not None:
            raise ValueError(
                "run_scoped streams do not use key=...; omit key or set "
                "mode='append_scan' for backlog/cursor streaming"
            )

    if active_rerun is not None:
        # Rerun override: always run-scoped source-run replay, regardless of
        # the configured mode. Passthrough still short-circuits the table read.
        if not getattr(table, "row_lineage", False):
            raise ValueError("Rerun streams require tables created with row_lineage=True")
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

    if mode == "run_scoped":
        # Run-scoped mode: read only the current run's rows via row lineage.
        # No ProgressStore, no cursor, no backlog draining. This treats the
        # table as a run-scoped store of results rather than a queue.
        if upstream_data is not None:
            df = _upstream_to_polars(upstream_data)
        elif context is None:
            raise RuntimeError(
                "run_scoped streams require an active workflow run context. "
                "Use mode='append_scan' for standalone / backlog streaming."
            )
        else:
            if getattr(table, "row_lineage", True) is False:
                raise ValueError(
                    "run_scoped streams require tables created with "
                    "row_lineage=True. Use mode='append_scan' for tables "
                    "without row lineage."
                )
            df = _scan_run_rows(
                table,
                context.run_id,
                node_slugs=source_node_slugs,
            )
        _merge_input_lineage(context, df)
        yield df
        return

    if mode != "append_scan":
        raise ValueError(f"Stream mode must be 'run_scoped' or 'append_scan', got {mode!r}")

    if key is None:
        raise ValueError("append_scan streams require key=...")

    # Create progress store if not provided
    if progress_store is None:
        progress_store = ProgressStore(table, key=key)

    # append_scan mode. Passthrough vs table-backed is decided by upstream_data:
    # - If upstream_data provided: passthrough (parent returned AppendResult)
    # - If upstream_data is None: read one claimed snapshot from the table
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
    slug_expr = " OR ".join(f"_ava_node_slug = {_sql_string(slug)}" for slug in unique_slugs)
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
        parent for parent in df["_ava_rerun_of"].drop_nulls().unique().to_list() if parent
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
