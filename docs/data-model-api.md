# Data Model API

Avalanche data models describe table schemas and group tables into namespaces.
The current release candidate supports:

- backend-neutral contracts: `NamespaceConfig`, `Namespace`, `Table`,
  `TableGroup`, and `ScanResult`;
- Iceberg-backed namespaces and tables;
- Lance-backed namespaces and tables when the optional Lance extra is installed;
- DataFramely schemas converted to backend-native table schemas;
- pydantic `BaseModel` schemas for model-first pipelines.

## Define schemas

Schemas are usually declared with DataFramely:

```python
import dataframely as dy

class DocumentSchema(dy.Schema):
    doc_id = dy.String(nullable=False)
    title = dy.String(nullable=False)
    body = dy.String(nullable=False)

class ChunkSchema(dy.Schema):
    chunk_id = dy.String(nullable=False)
    doc_id = dy.String(nullable=False)
    chunk_number = dy.Int32(nullable=False)
    text = dy.String(nullable=False)
```

DataFramely can validate or cast data before it is appended:

```python
validated = DocumentSchema.validate(df, cast=True)
```

## Pydantic model schemas

Tables can be declared directly from a pydantic `BaseModel` subclass anywhere a
DataFramely schema is accepted. This is the recommended source of truth for
model-first pipelines: one declaration serves the table schema and the
validation contract.

```python
from pydantic import BaseModel, Field

class Address(BaseModel):
    city: str = Field(description="City name")
    zip_code: str | None = None

class Person(BaseModel):
    id: int
    name: str = Field(description="Display name")
    address: Address
    tags: list[str]

class PeopleNamespace(ava.IcebergNs):
    ns_config = ava.IcebergNsConfig(name="people", base_location="...")

    people = ava.IcebergTable(schema=Person)
```

Type mapping is struct-first so the lake stays queryable: scalars map to native
columns, nested models to struct columns, and `list[...]` to list columns.
`Field(description=...)` becomes Arrow column documentation at every nesting
level. Fields that cannot map cleanly to Arrow (bare `Any`, non-optional
unions, heterogeneous dicts) fail loudly at declaration; opt into a JSON-string
column per field with the `ava.Json` marker:

```python
from typing import Annotated, Any

class Event(BaseModel):
    kind: str
    payload: Annotated[dict[str, Any], ava.Json]
```

Model-declared tables accept model instances in `append` and read back as
validated models:

```python
result = ns.people.append(Person(id=1, name="ada", address=Address(city="Toronto"), tags=[]))
ns.people.append([person_a, person_b])

people = ns.people.read_models()   # list[Person]
```

The returned `ava.AppendResult` is typed by the table's row model and adds
model accessors to the frame accessors:

- `to_models()` — the appended rows as validated model instances;
- `one()` — exactly one appended model; raises on zero or multiple rows
  (asserts the step's cardinality contract at the boundary);
- `one_or_none()` — the lenient variant for optional-output steps.

Row provenance columns (`_ava_*`) behave identically to DataFramely-schema'd
tables, and DataFramely remains fully supported for dataframe-native pipelines.

Use `ava.ModelStream` at workflow boundaries to inject row models instead of a
Polars DataFrame:

```python
@ava.step
def greet(person: Person = ava.ModelStream.one(ns.people)) -> str:
    return f"Hello, {person.name}"


@ava.step
def maybe_greet(
    person: Person | None = ava.ModelStream.one_or_none(ns.people),
) -> str | None:
    return None if person is None else f"Hello, {person.name}"


@ava.step
def greet_everyone(
    people: list[Person] = ava.ModelStream.all(ns.people),
) -> list[str]:
    return [f"Hello, {person.name}" for person in people]
```

`one()` requires exactly one row. `one_or_none()` returns `None` for zero rows
and rejects multiple rows. `all()` returns validated models in stable row order,
including an empty list for zero rows. These providers require a table declared
with a Pydantic model schema. Framework-owned `_ava_*` provenance columns are
removed from the model boundary internally. Cardinality and validation errors
include the table, workflow run, and upstream source node when available.

`ModelStream` supports the same durable options as `Stream`, for example
`ava.ModelStream.all(table, key="people_to_greet", mode="append_scan")`.
Passthrough, table-backed, local, distributed, and rerun execution preserve the
same read and progress semantics described below.

## Iceberg namespace

An Iceberg namespace groups declared tables under one catalog namespace and base
storage location.

```python
from pathlib import Path

import avalanche as ava

class ExampleNamespace(ava.IcebergNs):
    ns_config = ava.IcebergNsConfig(
        name="example",
        base_location=str(Path(".avalanche/examples/data-model") / "warehouse"),
    )

    document = ava.IcebergTable(schema=DocumentSchema)
    chunk = ava.IcebergTable(schema=ChunkSchema)

ns = ExampleNamespace(
    catalog="example",
    load_catalog_props={
        "type": "sql",
        "uri": "sqlite:///.avalanche/examples/data-model/catalog.db",
    },
)

ns.push()
```

`ns.push()` creates the namespace and any missing declared tables in the catalog.
After push, table declarations are bound to concrete backend tables.

## Append, scan, and read

Avalanche tables accept Polars, PyArrow tables, and PyArrow record batches.
Appending returns `ava.AppendResult`, which carries both the appended data and the
created backend version/snapshot id.

```python
import polars as pl

rows = pl.DataFrame(
    {
        "doc_id": ["doc-1"],
        "title": ["Release notes"],
        "body": ["Avalanche tables return AppendResult."],
    }
)

result = ns.document.append(rows)
print(result.snapshot_id)
print(result.to_polars())
print(result.to_dicts())
```

`AppendResult` keeps the appended rows available for downstream tasks. Use
`to_polars()`, `to_arrow()`, or `to_dicts()` depending on the shape the next step
needs.

By default, Avalanche appends framework-owned row provenance columns to every
Iceberg and Lance table:

- `_ava_updated_at`: UTC timestamp for the write operation;
- `_ava_run_id`: current workflow run id, when the append happens inside a
  workflow;
- `_ava_rerun_of`: source run id when the current run is an explicit rerun;
- `_ava_workflow_name`: current workflow name;
- `_ava_node_id`: current node invocation id;
- `_ava_node_name`: current node function name;
- `_ava_node_slug`: stable rerun-addressable node id;
- `_ava_lineage_vector`: compact JSON vector clock of upstream node slug to
  producing run id, plus the current writer;
- `_ava_ctx_metadata`: compact JSON object for additional
  `RunContext.metadata` fields supplied by the caller or platform.

These columns are write provenance plus run-level lineage for reruns, not full
entity lineage: Avalanche does not infer `created_at`, primary keys, or parent
row ids. Disable the default columns for a table with `row_lineage=False`:

```python
document = ava.IcebergTable(schema=DocumentSchema, row_lineage=False)
```

Use the backend-neutral scan/read contract for portable code:

```python
selected = ns.document.scan(columns=["doc_id", "title"]).to_polars()
all_rows = ns.document.read()
```

Iceberg also proxies PyIceberg table methods and attributes where available, such
as `history()`, `snapshots()`, `metadata`, and backend-specific scan options.

## Backend-neutral contracts

Use the neutral `ava.Table` and `ava.Namespace` contracts when code should not
care whether data is backed by Iceberg or Lance.

```python
def consume(table: ava.Table) -> int:
    rows = table.read()
    return len(rows)


def deploy(namespace: ava.Namespace) -> None:
    namespace.push()
```

Common table members include:

- `identifier`: full `namespace.table` identifier;
- `location`: backend storage location;
- `schema_fields`: field names in declaration order, including default
  `_ava_*` row provenance fields unless `row_lineage=False`;
- `current_version_id`: current snapshot/version id, if one exists;
- `append(...)`: append rows and return `ava.AppendResult`;
- `scan(...)`: create a backend-neutral `ScanResult`;
- `read()`: read the current contents as a Polars DataFrame.

Common namespace members include:

- `name` and `base_location` from `ns_config`;
- `location`: namespace storage location;
- `push()`: create/update namespace and tables;
- `drop(drop_tables=False)`: drop namespace metadata and optionally tables;
- `list_tables()`: declared table names.

## Lance namespace

Lance support uses the same namespace/table shape with Lance-specific classes.
Install the optional dependency before running Lance table operations:

```bash
uv sync --extra lance
```

```python
class ExampleLanceNamespace(ava.LanceNamespace):
    ns_config = ava.LanceNamespaceConfig(
        name="example_lance",
        base_location=".avalanche/examples/lance",
    )

    document = ava.LanceTable(schema=DocumentSchema)
    chunk = ava.LanceTable(schema=ChunkSchema)

lance_ns = ExampleLanceNamespace()
lance_ns.push()
```

Lance tables support the neutral `append`, `scan`, `read`, `history`, and
`current_version_id` paths used by stream/cursor progress tracking. Lance
`append_scan` currently supports replaying one data-producing version from its
direct parent, not arbitrary version ranges.

## Streams, reruns, and cursors

`ava.Stream(table)` injects a `polars.DataFrame` into a task parameter. By
default it is run-scoped (reads the current run's rows via row lineage). Pass
`key=...` with `mode="append_scan"` for incremental backlog draining.

```python
@ava.step
def chunk_documents(
    _loaded: object = None,
    docs = ava.Stream(ns.document, key="documents_to_chunks", mode="append_scan"),
    *,
    dest = ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)
```

`ava.Cursor(table, key=...)` stores manual checkpoint state in table metadata.
Use it when a task needs custom progress state, a different source table than its
destination table, or coordination across multiple tables.

### Stream modes

`ava.Stream` has an explicit **durable read mode** that decides how it reads
from the table when the data is not already available in memory:

1. **`run_scoped`** (default): read the rows this workflow run produced, matched
   by row-lineage columns (`_ava_run_id` and the upstream producer's
   `_ava_node_slug`). There is no `ProgressStore`, no cursor, and no backlog
   draining. `key` is not used. This treats the table as a run-scoped store of
   results — the common shape for multi-agent / checkpoint pipelines.
2. **`append_scan`**: queue/backlog mode. The stream claims one pending backend
   snapshot for `key` via `ProgressStore`, reads only that snapshot's appended
   files, and marks the snapshot done or failed. Requires `key=...`.

```python
# Default: run-scoped, no cursor.
docs = ava.Stream(ns.document)

# Backlog / incremental queue.
docs = ava.Stream(ns.document, key="documents_to_chunks", mode="append_scan")
```

**Passthrough is not a mode.** When an upstream node returns `AppendResult`, its
rows are passed directly to the downstream stream in the same run, skipping the
table read in either mode. For `append_scan` the corresponding snapshot is still
claimed and completed in `ProgressStore`; for `run_scoped` there is no progress
bookkeeping.

**Rerun overrides the mode.** When `Workflow.run(rerun=ava.Rerun(...))` is
active, every stream reads all rows for the source `run_id` via row-lineage
columns and bypasses `ProgressStore` completely, regardless of `mode`, so a
rerun never consumes or advances ordinary stream backlog.

> Breaking change: the default is now `run_scoped`. Code that relied on the old
> backlog-draining behavior must pass `mode="append_scan"` (and keep `key`).
> `key=` is only valid with `append_scan`.

Run-scoped and rerun scans require `row_lineage=True` on the table. Rerun mode
enforces that requirement before any live `AppendResult` passthrough. Produced
rows keep payload and producer-version lineage in `_ava_rerun_of`,
`_ava_node_slug`, and `_ava_lineage_vector`.

If the source run is itself a rerun, Avalanche overlays newer rows for the same
`_ava_node_slug` ahead of older parent-run rows. A lazy intermediate rerun may
consume a table without writing rows to it, so row columns on that table cannot
represent the sparse ancestry edge. Avalanche records one minimal durable table
property linking that rerun to its source run for each consumed table. This
property is only the missing ancestry edge; it is not a full manifest, stale
bit, or lineage hash.

### Stream pacing and executors

Table-backed streams claim **one pending snapshot per run**, oldest first. A
backlog of N snapshots drains over N runs; schedule repeated runs (or an
operator loop) to catch up. This keeps per-snapshot processing atomic: each
snapshot is claimed, processed, and marked done or failed as a unit.

Stream steps run on any executor. On `RayExecutor`, table handles are pickled
as a reconnect recipe (catalog address + table identifier) and each worker
opens its own catalog connection. This requires a catalog reachable from
worker processes — a file- or server-backed catalog. In-memory catalogs
(`sqlite:///:memory:`) are per-process and fail at submit time with a clear
error.

Rerun stream reads use the backend-neutral `scan(filter=...)` path against
row-lineage columns. For Iceberg this maps to the current table snapshot and
PyIceberg filtering. For Lance it maps to Lance scans over the current dataset
version. Ordinary table-backed stream pacing still uses the backend's
append-scan/direct-version mechanics to process one pending snapshot per run.

## Current caveats

- Schema migration commands are not part of this release candidate. Do not use
  older `icy migrate ...` or `icy lake push` examples.
- `IcebergTable.merge(...)` is not implemented yet; use append/read/overwrite or
  backend-native PyIceberg operations as appropriate.
- Production catalog, auth, and cloud deployment guidance are outside this
  handoff. The examples use local SQLite catalogs and local filesystem paths.

See [`examples/stream_pattern.py`](../examples/stream_pattern.py) and
[`examples/cursor_pattern.py`](../examples/cursor_pattern.py) for runnable table,
stream, and cursor examples.
