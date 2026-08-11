# Data Model and Storage API

Avalanche tables group schemas in namespaces. Use Iceberg by default, or Lance with
`uv sync --extra lance`.

## Define a schema

Use a DataFramely schema for dataframe-first pipelines:

```python
import dataframely as dy


class DocumentSchema(dy.Schema):
    doc_id = dy.String(nullable=False)
    title = dy.String(nullable=False)
    body = dy.String(nullable=False)
```

Validate before writing when needed:

```python
validated = DocumentSchema.validate(df, cast=True)
```

Use a Pydantic model for model-first pipelines. Nested models and `list[...]`
fields become nested table columns.

```python
from pydantic import BaseModel, Field


class Address(BaseModel):
    city: str = Field(description="City name")
    zip_code: str | None = None


class Person(BaseModel):
    id: int
    name: str
    address: Address
    tags: list[str]
```

Use `ava.Json` for a field that should be stored as JSON rather than a native
column:

```python
from typing import Annotated, Any


class Event(BaseModel):
    kind: str
    payload: Annotated[dict[str, Any], ava.Json]
```

## Create a namespace

Declare tables as class attributes, instantiate the namespace, then call
`push()` to create missing tables.

```python
from pathlib import Path

import avalanche as ava


class ExampleNamespace(ava.IcebergNs):
    ns_config = ava.IcebergNsConfig(
        name="example",
        base_location=str(Path(".avalanche/examples/data-model") / "warehouse"),
    )

    documents = ava.IcebergTable(schema=DocumentSchema)
    people = ava.IcebergTable(schema=Person)


ns = ExampleNamespace(
    catalog="example",
    load_catalog_props={
        "type": "sql",
        "uri": "sqlite:///.avalanche/examples/data-model/catalog.db",
    },
)
ns.push()
```

For a Pydantic model table, pass the model as `schema`, as with
`ExampleNamespace.people`.

Lance uses the same shape:

```python
class ExampleLanceNamespace(ava.LanceNamespace):
    ns_config = ava.LanceNamespaceConfig(
        name="example_lance",
        base_location=".avalanche/examples/lance",
    )

    documents = ava.LanceTable(schema=DocumentSchema)


lance_ns = ExampleLanceNamespace()
lance_ns.push()
```

## Write and read rows

Tables accept Polars dataframes, PyArrow tables, and record batches.

```python
import polars as pl

rows = pl.DataFrame(
    {
        "doc_id": ["doc-1"],
        "title": ["Release notes"],
        "body": ["Avalanche tables return AppendResult."],
    }
)

result = ns.documents.append(rows)
print(result.snapshot_id)
print(result.to_polars())

selected = ns.documents.scan(columns=["doc_id", "title"]).to_polars()
all_rows = ns.documents.read()
```

`append()` returns `ava.AppendResult`. Use `to_polars()`, `to_arrow()`, or
`to_dicts()` for the appended rows.

Model-declared tables accept model instances and can return validated models:

```python
ns.people.append(Person(id=1, name="Ada", address=Address(city="Toronto"), tags=[]))
people = ns.people.read_models()  # list[Person]
```

`AppendResult.to_models()` returns appended models. `one()` requires exactly one
model; `one_or_none()` permits no model.

## Portable table API

Use `ava.Table` and `ava.Namespace` when application code should work with either
backend.

```python
def consume(table: ava.Table) -> int:
    return len(table.read())


def deploy(namespace: ava.Namespace) -> None:
    namespace.push()
```

| Type | Common members |
| --- | --- |
| `Table` | `identifier`, `location`, `schema_fields`, `current_version_id`, `append(...)`, `scan(...)`, `read()` |
| `Namespace` | `name`, `base_location`, `location`, `push()`, `drop(drop_tables=False)`, `list_tables()` |

Iceberg also exposes available PyIceberg table methods such as `history()` and
`snapshots()`. Lance supports `append`, `scan`, `read`, `history`, and
`current_version_id`.

## Row provenance

Iceberg and Lance add `_ava_*` provenance columns by default, including the write
time, workflow run, producing node, and rerun lineage. Disable them for a table
with `row_lineage=False`:

```python
documents = ava.IcebergTable(schema=DocumentSchema, row_lineage=False)
```

Keep row lineage enabled when using run-scoped streams or reruns.

## Streams and cursors

`ava.Stream(table)` injects a `polars.DataFrame` into a node. The default mode
reads rows produced by the current workflow run. Use `append_scan` for incremental
snapshot processing.

```python
@ava.step
def chunk_documents(
    _loaded: object = None,
    docs=ava.Stream(ns.documents, key="documents_to_chunks", mode="append_scan"),
    *,
    dest=ns.chunks,
):
    return dest.append(process_documents_to_chunks(docs))
```

| Need | API |
| --- | --- |
| Current-run rows | `ava.Stream(table)` |
| Incremental backlog | `ava.Stream(table, key="...", mode="append_scan")` |
| Model rows | `ava.ModelStream.one(table)`, `.one_or_none(table)`, or `.all(table)` |
| Custom checkpoint state | `ava.Cursor(table, key="...")` |

`append_scan` processes one pending snapshot per run. `ModelStream` requires a
Pydantic model table and accepts the same durable options as `Stream`.

## Current caveats

- Schema migration commands are not included in this release candidate.
- `IcebergTable.merge(...)` is not implemented; use append/read/overwrite or
  backend-native PyIceberg operations.
- Examples use local SQLite catalogs and local filesystem paths.

See [`examples/stream_pattern.py`](../examples/stream_pattern.py) and
[`examples/cursor_pattern.py`](../examples/cursor_pattern.py) for runnable
examples.
