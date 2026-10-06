# Lance

Avalanche 0.7.0. Imports: `from avalanche import LanceNamespace, LanceNamespaceConfig, LanceTable`.

## Installation

Native Lance is optional and loaded on dataset access:

```sh
uv add 'avalanche-ai[lance]'
# In a source checkout:
uv sync --extra lance
```

Dataset operations without it raise `ImportError`. Declaring tables or creating namespace directories does not require the native dependency.

## Namespace and configuration

```python
LanceNamespaceConfig(name: str, base_location: str, properties: dict[str, str] | None = None)
LanceNamespace()
```

Declare `ns_config` and public table attributes on a subclass:

```python
from pydantic import BaseModel
from avalanche import LanceNamespace, LanceNamespaceConfig, LanceTable

class Document(BaseModel):
    doc_id: str
    title: str

class Documents(LanceNamespace):
    ns_config = LanceNamespaceConfig(name="documents", base_location="/tmp/lance")
    documents = LanceTable(Document)

ns = Documents()
```

| Configuration field | Meaning |
| --- | --- |
| `name: str` | Required namespace name and table-identifier prefix. |
| `base_location: str` | Required local filesystem root. Object-store URIs are not supported by this namespace implementation. |
| `properties: dict[str, str]` | Constructor default `None`, stored as `{}`; currently not applied by `push()`. |

Configuration is a plain mutable object. There is no catalog argument. The namespace exposes `ns_config`, `name`, `base_location`, `location` (root joined with name), and its declared tables. Missing class-level configuration raises `ValueError`. Declarations are shared mutable table objects: a second instance of the same namespace class rebinds them rather than creating independent handles.

```python
ns.push() -> None
ns.list_tables() -> list[str]
ns.drop(*, drop_tables: bool = False) -> None
```

`push()` prepares namespace/table directories; it does not create an empty dataset or migrate schemas. The first append (or committed metadata update) creates the dataset. Filesystem errors propagate.

`list_tables()` lists declared table names, not datasets discovered on disk.

`drop(drop_tables=True)` attempts to recursively delete declared table directories **including their data**, then remove the namespace directory. Deletion is best-effort: table-directory deletion ignores filesystem errors, and namespace-directory removal suppresses every `OSError`, including permission failures. A normal return does not prove that datasets or the namespace were deleted; verify their absence when required. With the default `False`, table contents remain. Undeclared directories are not recursively deleted.

## Table declaration and fields

```python
LanceTable(schema: Any, *, row_lineage: bool = True)
```

`schema` is required: an Arrow `pa.Schema` instance, DataFramely `Schema` class, or Pydantic `BaseModel` class. PyIceberg schemas are not accepted (`TypeError`). Pydantic field support and model-input constraints follow [schema declarations](iceberg.md#table-and-schema-declarations); DataFramely schemas use their Arrow representation, without Iceberg's scalar-only conversion restriction. Appending does not run DataFramely validation.

| Usable field/property | Meaning |
| --- | --- |
| `schema: pa.Schema` | Normalized Arrow schema, including enabled provenance. |
| `row_lineage: bool` | Default `True`; adds provenance on writes. Reserved-column collisions raise `ValueError` at declaration. |
| `row_model` | Declared Pydantic model class, otherwise `None`. |
| `identifier: str` | Bound `namespace.table`. |
| `location: str` | Bound local dataset directory. |
| `schema_fields: tuple[str, ...]` | Ordered storage column names, including provenance. |
| `current_version_id: int \| None` | Latest dataset version, including metadata-only commits; `None` without a dataset. |
| `properties: dict[str, str]` | Copy of current dataset metadata; `{}` without a dataset. |

## Append and read

Here `FrameInput` means `pl.DataFrame | pa.Table | pa.RecordBatch | BaseModel | Sequence[BaseModel]`, not an exported alias.

```python
table.append(df: FrameInput) -> AppendResult
table.read() -> pl.DataFrame
table.read_models() -> list[BaseModel]
```

`append` requires a bound nonempty location (`AttributeError` otherwise); call `ns.push()` to prepare directories. It casts to the declared Arrow schema and appends, attaching/replacing provenance when enabled. Initial dataset creation is filesystem-locked. It returns the shared [AppendResult](iceberg.md#append-result), with the committed Lance version and the cast appended rows. There is no Avalanche commit-conflict retry loop for Lance append.

Model inputs require a Pydantic-declared table and matching model instances (`TypeError` otherwise); empty model sequences raise `ValueError`. Empty Arrow/Polars input behavior is backend-dependent. Arrow casts and native storage errors propagate. Frame input does not undergo Pydantic validation.

`read` materializes the full current dataset as Polars. `read_models` validates rows against the declared Pydantic class, omitting unrelated columns; without that declaration it raises `TypeError`. Validation errors propagate. A bound table without a dataset reads as an empty table with its declared schema.

## Scans

Author-facing call form (the implementation rejects positional and extra keyword arguments):

```python
table.scan(*, columns: list[str] | None = None, filter: Any = None, limit: int | None = None)
```

`columns=None` selects all fields, `filter=None` applies no filter, and `limit=None` imposes no limit. Filters pass to native Lance unchanged. Extra arguments, including `snapshot_id`, `selected_fields`, and `row_filter`, raise `TypeError`; full-table historical scans are not supported. Backend selection/filter errors propagate.

The returned scan reads on materialization, not at creation, so it is not pinned to a version. `to_arrow() -> pa.Table` and `to_polars() -> pl.DataFrame` materialize selected rows. `to_arrow_batch_reader() -> pa.RecordBatchReader` materializes the entire Arrow table before creating its reader, rather than guaranteeing bounded-memory streaming. Unbound materialization raises `AttributeError`. Without a dataset, projection/limit apply to an empty declared-schema table and no filter is evaluated.

```python
table.append_scan(
    *,
    start_snapshot_id: int | None = None,
    snapshot_id: int | None = None,
    columns: list[str] | None = None,
    filter: Any = None,
    selected_fields: tuple[str, ...] = ("*",),
    row_filter: Any = None,
    limit: int | None = None,
)
```

This returns a scan with the same three conversion methods, reading rows introduced by **one** append/overwrite version. `snapshot_id=None` chooses the current version at scan construction; no existing version raises `ValueError`. A non-`None` baseline must equal that version's direct parent, otherwise `NotImplementedError`. Even with no baseline, this does not read all accumulated rows.

`columns` takes precedence over `selected_fields`; a non-wildcard `selected_fields` supplies the projection when `columns=None`. `filter` takes precedence over `row_filter`. `limit=None` is unlimited; other limits apply across the selected fragments. Missing transaction records, non-data versions, or no matching fragments yield an empty projected declared-schema table. Invalid/expired version and native read errors may propagate.

Lance [streams](streams.md) inherit these single-version replay constraints. [Cursor](cursors.md) metadata updates are supported, but cursor transaction row append is not supported on Lance. Workers accessing a bound table must share its local filesystem location.
