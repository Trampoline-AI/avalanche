# Cursors

Avalanche 0.7.0. Import: `from avalanche import Cursor`.

## Cursor

```python
Cursor(table, *, key: str)
cursor.get() -> Any | None
cursor.set(value: Any) -> None
cursor.transaction()  # returns a transaction context manager
cursor.tx()           # shorthand for transaction()
```

A durable, author-managed checkpoint stored in table properties. It is used directly, including as a node parameter default; it is not an injected DataFrame or [Stream](streams.md).

| Parameter/public field | Meaning |
| --- | --- |
| `table` | Required [Iceberg](iceberg.md) or [Lance](lance.md) table; must supply table properties and transactions. |
| `key: str` | Required checkpoint name, matching `^[a-z][a-z0-9_]*$`; invalid spelling raises `ValueError`. Multiple keys maintain independent checkpoints on one table. |

Both arguments remain available as `cursor.table` and `cursor.key`. The persisted property name is `avalanche.cursor.<key>`.

`get()` returns the current property value or `None` when absent. `set(value)` stages `str(value)`, not typed or JSON-encoded data. Parse the returned string when numeric or structured state is required. `get()` neither refreshes the table nor reads staged transaction updates; for a potentially stale Iceberg handle call `table.refresh()` before reading.

`set()` requires an active `with cursor.transaction():` block; otherwise it raises `RuntimeError`. It returns `None` and does not commit independently.

## Transactions

Author-facing call forms:

```python
with cursor.transaction() as tx:
    tx.append(arrow_table)
    cursor.set(last_id)

with cursor.tx() as tx:
    tx.set_properties(description="imported")
```

The context manager enters the table transaction and yields a wrapper with these operations:

| Call | Behavior and return |
| --- | --- |
| `tx.append(data: Any) -> Any` | On Iceberg, appends Arrow-compatible data in the same transaction as the checkpoint. Adds active-run provenance when enabled and casts to the table schema. Returns the underlying transaction's result, **not** an Avalanche `AppendResult`. Prefer `pa.Table` input (for example `frame.to_arrow()`); this is not `table.append`'s model-input API. |
| `tx.set_properties(**kwargs: Any) -> None` | Stages table-property updates in the underlying transaction. Use `cursor.set` for the cursor's own property. |

Successful context exit commits staged writes and properties together according to the backend transaction. Exceptional exit delegates rollback/commit handling to that backend; active cursor state is always cleared. Cast, backend write, and commit/conflict errors propagate; the cursor wrapper adds no retry loop. There is no nested/reentrant transaction coordination: do not overlap transactions on the same cursor.

Iceberg requires `namespace.push()` before transaction access (`AttributeError` otherwise). On Lance, cursor transactions support metadata only: `cursor.set` and `tx.set_properties` work, but `tx.append` raises `AttributeError`. Lance stringifies property values, may create an empty dataset for the first metadata commit, and commits no update on exceptional exit. A native Lance commit `OSError` becomes `CommitFailedException`; metadata commits also advance the dataset version.

Use `table.append(...)` separately only when an atomic append-and-checkpoint transaction is not required. A cursor does not automatically track stream consumption or checkpoint an entire workflow.
