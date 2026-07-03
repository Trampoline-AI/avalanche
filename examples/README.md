# Avalanche Examples

This directory contains the canonical examples. The canonical example scripts are
covered by `test/example_smoke_test.py`.

Run examples from the repository root:

```bash
uv run python examples/<example_name>.py
```

By default, examples that need storage write under `.avalanche/examples/`. To
redirect artifacts, set `AVALANCHE_EXAMPLE_ROOT`:

```bash
AVALANCHE_EXAMPLE_ROOT=/path/to/tempdir uv run python examples/stream_pattern.py
```

## Canonical Examples

### `complex_dag_pattern.py`

Simplest local flow execution path. It demonstrates the Python DAG API,
explicit data passing, fan-out, fan-in, and `ava.LocalExecutor` without requiring
Iceberg, Ray, the operator, or the TUI.

```bash
uv run python examples/complex_dag_pattern.py
```

### `stream_pattern.py`

Stream-based incremental processing with local Iceberg tables. It uses the
current provider API:

```python
@ava.step
def chunk_documents(
    docs: pl.DataFrame = ava.Stream(ns.document, key="documents_to_chunks"),
    *,
    dest=ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)
```

Each consumer edge gets a unique Stream key, and the runtime injects a
`polars.DataFrame` for the claimed snapshot.

```bash
uv run python examples/stream_pattern.py
```

### `cursor_pattern.py`

Manual checkpoint control for advanced incremental flows. It demonstrates a
Cursor that tracks a source table snapshot while writing to a destination table,
model-specific cursors, and a multi-table sync checkpoint.

```bash
uv run python examples/cursor_pattern.py
```

### `operator_workflow.py`

Flow file intended for the local operator and connected TUI path. It can also
run directly with the local executor.

```bash
uv run python examples/operator_workflow.py
```

Start the operator against the canonical examples directory:

```bash
uv run ava operator --flows examples --port 7433
```

In another terminal, connect the TUI:

```bash
uv run ava tui --connect localhost:7433
```

The TUI consumes the operator's gRPC flow list; it does not import example files
directly.

The TUI also supports mock mode without an operator:

```bash
uv run python -m avalanche.tui
```

## Notes

- `ava.Stream(table, key=...)` is a provider marker, not an object you call with
  `.read()`.
- Use one unique Stream key per consumer edge.
- Local example artifacts are ignored by git through `.avalanche/`.
- Only the examples listed here are part of the smoke-tested onboarding path.
