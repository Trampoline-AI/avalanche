# Data Model API

Avalanche data models describe table schemas and group tables into namespaces.
The current release candidate supports:

- backend-neutral contracts: `NamespaceConfig`, `Namespace`, `Table`,
  `TableGroup`, and `ScanResult`;
- Iceberg-backed namespaces and tables;
- Lance-backed namespaces and tables when the optional Lance extra is installed;
- DataFramely schemas converted to backend-native table schemas.

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
- `schema_fields`: field names in declaration order;
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

## Streams and cursors

`ava.Stream(table, key=...)` consumes backend table versions incrementally and
injects a `polars.DataFrame` into a task parameter.

```python
@ava.step
def chunk_documents(
    _loaded: object = None,
    docs = ava.Stream(ns.document, key="documents_to_chunks"),
    *,
    dest = ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)
```

`ava.Cursor(table, key=...)` stores manual checkpoint state in table metadata.
Use it when a task needs custom progress state, a different source table than its
destination table, or coordination across multiple tables.

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
