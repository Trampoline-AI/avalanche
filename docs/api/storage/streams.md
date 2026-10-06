# Streams

Avalanche 0.7.0. Import: `from avalanche import Stream`.

## Stream

```python
Stream(table, *, key: str | None = None, mode: Literal["run_scoped", "append_scan"] = "run_scoped")
```

A parameter-default marker for a [workflow primitive](../workflows.md), not an iterator or a reader object. Construction does not read storage. Execution replaces the marker with one `polars.DataFrame`.

Author-facing parameter declaration:

```python
def process(documents: pl.DataFrame = Stream(ns.documents)):
    ...

# Durable incremental consumer:
def process_pending(
    documents: pl.DataFrame = Stream(
        ns.documents, key="documents_to_chunks", mode="append_scan"
    ),
):
    ...
```

| Parameter/public field | Default and meaning |
| --- | --- |
| `table` | Required [Iceberg](iceberg.md) or [Lance](lance.md) table to consume. Backend read/replay restrictions apply. |
| `key: str \| None` | `None`. Required for `append_scan`; rejected for `run_scoped`. Identifies an independent durable consumer on this table. |
| `mode` | `"run_scoped"`; alternative `"append_scan"`. Other values raise `ValueError`. |

Missing `key` in append-scan mode or non-`None` `key` in run-scoped mode raises `ValueError`. The marker itself does not otherwise validate key spelling, but append-scan consumption requires `^[a-z][a-z0-9_]*$` and raises `ValueError` for an invalid key. Use a stable lowercase snake-case key for a durable consumer.

## Injected data and consumption

| Mode | Table-backed read behavior |
| --- | --- |
| `run_scoped` | Reads rows for the active run, constrained to the upstream producer(s) when available. Does not drain older runs' backlog. Requires an active [run context](../inputs-context.md) (`RuntimeError` otherwise), and rejects tables explicitly configured with `row_lineage=False` (`ValueError`). |
| `append_scan` | Consumes one pending data snapshot/version per execution, using its parent as the exclusive boundary. It does not drain every pending snapshot in one call. No pending data produces an empty DataFrame; do not assume it has a schema. Supports tables without row lineage. |

A corresponding upstream [AppendResult](iceberg.md#append-result) can supply its appended rows directly instead of reading storage. Stream parameters consume upstream result positions; a plain DataFrame or unrelated return value is not an append-result passthrough and falls back to a table-backed read. Run-scoped passthrough does not perform the table-read checks above.

For append-scan mode, successful consumer completion attempts to record consumption and advance the checkpoint, including when rows came from an upstream append result. An ordinary consumer exception is re-raised after attempting to record failure. Exhausted storage commit conflicts during acknowledgment or checkpoint advancement are suppressed: a consumer can return successfully without durable acknowledgment, leaving the snapshot eligible for replay after its processing lease expires. Other claim/read/persistence errors can propagate; a snapshot that cannot be claimed can raise `RuntimeError`. Consumption acknowledgment and arbitrary downstream writes are not one atomic transaction, so this does not promise exactly-once side effects. Do not treat successful consumer return as proof that consumption was recorded.

The injected frame retains provenance columns. Consumed provenance contributes to the active run context for subsequent writes. Frames are materialized, not lazy iterators, and no extra sort or primary-key deduplication is performed.

Streams only read: returning a frame does not persist it. Call `destination.append(frame)` explicitly; returning its `AppendResult` supplies appended rows to downstream consumers. For an author-managed checkpoint and atomic Iceberg append, see [cursors](cursors.md).
