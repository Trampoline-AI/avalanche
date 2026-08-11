# Avalanche Examples

This directory contains the canonical examples. The standalone Python scripts
are covered by `test/example_smoke_test.py`.

Run the examples from this directory:

```bash
uv run ava dev
```

This starts the local operator and browser UI. The operator discovers the
workflows in `examples/`; select any discovered workflow in the UI to start a
run.

## Canonical Examples

### [`customer_feedback_review/`](customer_feedback_review/)

Production-shaped agentic review workflow. It analyzes a customer-feedback
workbook with parallel theme and risk agents, validates their reports
deterministically, then publishes an Excel product review and Word executive
brief. See its [README](customer_feedback_review/README.md) for the full flow.

### `complex_dag_pattern.py`

Simplest local flow execution path. It demonstrates the Python DAG API,
explicit data passing, fan-out, fan-in, and `ava.LocalExecutor` without requiring
Iceberg, Ray, the operator, or the TUI.


### `stream_pattern.py`

Stream-based incremental processing with local Iceberg tables. It uses the
current provider API:

```python
@ava.step
def chunk_documents(
    docs: pl.DataFrame = ava.Stream(
        ns.document, key="documents_to_chunks", mode="append_scan"
    ),
    *,
    dest=ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)
```

This example uses append-scan streams (`mode="append_scan"`): each consumer edge
gets a unique Stream `key`, and the runtime injects a `polars.DataFrame` for the
claimed snapshot. The default `ava.Stream(table)` is run-scoped and takes no key.


### `cursor_pattern.py`

Manual checkpoint control for advanced incremental flows. It demonstrates a
Cursor that tracks a source table snapshot while writing to a destination table,
model-specific cursors, and a multi-table sync checkpoint.


### `operator_workflow.py`

Flow file for the local operator and connected UI path. It is discovered along
with the other examples when `uv run ava dev` runs from this directory.

## Notes

- `ava.Stream(table)` is a provider marker, not an object you call with
  `.read()`. It defaults to run-scoped reads.
- For backlog/queue streaming use `ava.Stream(table, key="...", mode="append_scan")`
  with one unique key per consumer edge. `key` is only valid with `append_scan`.
- Local example artifacts are ignored by git through `.avalanche/`.
- The standalone scripts listed here are part of the smoke-tested onboarding path.
