# Iceberg

Avalanche 0.7.0. Imports: `from avalanche import IcebergNamespace, IcebergNsConfig, IcebergTable, AppendResult`.

## Namespace and configuration

```python
IcebergNsConfig(name: str, base_location: str, properties: dict[str, str] | None = None)
IcebergNamespace(*, catalog: str | Catalog | None = None, load_catalog_props: dict | None = None)
```

`Catalog` is a PyIceberg catalog. Declare configuration and tables on a namespace subclass:

```python
from pydantic import BaseModel
from avalanche import IcebergNamespace, IcebergNsConfig, IcebergTable

class Document(BaseModel):
    doc_id: str
    title: str

class Documents(IcebergNamespace):
    ns_config = IcebergNsConfig(name="documents", base_location="/tmp/warehouse")
    documents = IcebergTable(Document)

ns = Documents(
    catalog="local",
    load_catalog_props={"type": "sql", "uri": "sqlite:////tmp/catalog.db"},
)
```

| Parameter/field | Default and meaning |
| --- | --- |
| `name: str` | Required namespace name and table-identifier prefix. |
| `base_location: str` | Required storage root path or backend-supported URI. |
| `properties: dict[str, str]` | Constructor default `None`, stored as `{}`. Currently not applied by `push()`; use `load_catalog_props` for catalog configuration. |
| `catalog` | `None`: load a catalog named after the namespace using PyIceberg configuration. A string loads that named catalog with `load_catalog_props`. A catalog object is used directly. |
| `load_catalog_props` | `None`; forwarded only when `catalog` is a string. Nonempty properties with a catalog object raise `ValueError`. |

Configuration is a mutable plain object, not a Pydantic model. The namespace exposes `ns_config`, `name`, `base_location`, `location` (root joined with name), `catalog`, and declared table attributes. Missing class-level configuration or an invalid catalog argument raises `ValueError`; catalog configuration/loading errors propagate.

Namespace construction binds declarations, but does not create tables. Table declarations are shared mutable objects: instantiating the same namespace class again rebinds them, rather than making independent handles.

```python
ns.push() -> None
ns.list_tables() -> list[str]
ns.drop(*, drop_tables: bool = False) -> None
```

- `push()` creates missing namespace/tables and loads existing tables. It does not migrate schemas, replace data, or apply configuration `properties`. Call it before table operations.
- `list_tables()` returns declared table names, not a live catalog listing.
- `drop()` removes the catalog namespace. With `drop_tables=True`, it first drops declared tables only and clears their loaded handles. Missing namespace/table errors are tolerated; remaining undeclared tables may prevent namespace removal. This is catalog removal, not guaranteed physical file deletion. Other catalog errors propagate.

## Table and schema declarations

```python
IcebergTable(schema: Any, *, row_lineage: bool = True)
```

`schema` is required: a PyIceberg `Schema` instance, DataFramely `Schema` class, or Pydantic `BaseModel` class. Raw Arrow schemas are not accepted (`TypeError`). For example, a DataFramely declaration can use `class DocumentSchema(dy.Schema):` with `doc_id = dy.String(nullable=False)` and pass `DocumentSchema` to the table constructor.

Simple Pydantic fields support strings, booleans, integers, floats, dates, datetimes, bytes, string/integer enums, nested models, lists, and nullable `T | None`. Field names/order follow the model, not aliases; nullability follows annotations, not defaults. Unsupported fields such as unmarked dictionary/object types, `Any`, bare lists, and multi-alternative unions raise `UnsupportedModelFieldError` (a `ValueError` subclass). DataFramely's Iceberg conversion supports scalar integer, floating-point, string, binary, boolean, date and timestamp fields; unsupported nested/list conversions raise `NotImplementedError`.

| Usable field/property | Meaning |
| --- | --- |
| `schema` | Normalized PyIceberg schema attribute, not a callable method. |
| `row_lineage: bool` | Default `True`; adds reserved provenance columns to the schema and writes. Reserved-name collisions raise `ValueError`. |
| `row_model` | Declared Pydantic class, otherwise `None`. |
| `identifier: str` | Bound `namespace.table` name. |
| `location: str` | Namespace location joined with table name; empty before binding. |
| `schema_fields: tuple[str, ...]` | Ordered schema column names, including provenance. |
| `current_version_id: int \| None` | Cached current snapshot ID; `None` before loading or before any snapshot. Reading it does not refresh metadata. |

Lineage-enabled writes replace incoming provenance with the active run's provenance; outside a run only the write timestamp is populated. `row_lineage=False` disables these additions. Existing stored schemas are not upgraded by changing a declaration and calling `push()`.

## Append and read

Here `FrameInput` means `pl.DataFrame | pa.Table | pa.RecordBatch | BaseModel | Sequence[BaseModel]`, not an exported alias.

```python
table.append(df: FrameInput) -> AppendResult
table.read() -> pl.DataFrame
table.read_models() -> list[BaseModel]
```

`append` refreshes metadata, casts to the loaded storage schema, and commits the supplied rows. It returns [AppendResult](#append-result) containing those rows, not the entire table. Model input requires a Pydantic-declared table and instances of its declared model (subclasses accepted); incompatible input raises `TypeError`, an empty model sequence raises `ValueError`. Empty frame behavior is backend-dependent. Frame appends do not run DataFramely validation or Pydantic model validation. Arrow cast/type/nullability and storage errors propagate.

`read()` materializes the current full scan as Polars; `read_models()` validates rows as the declared Pydantic model, excluding unrelated columns. Without a Pydantic declaration `read_models()` raises `TypeError`; model-validation errors propagate.

Unpushed table operations raise `AttributeError`. Appends through a single wrapper are serialized; catalog commit conflicts are retried with refreshed metadata for up to 30 seconds, then `CommitFailedException` propagates.

## Scans and current snapshots

Author-facing call forms (additional native PyIceberg scan arguments are accepted):

```python
table.scan(*, columns: list[str] | None = None, filter: Any = None)
table.scan(snapshot_id=snapshot_id)
table.current_snapshot()
table.refresh()
```

`scan` refreshes metadata and returns a scan result. `columns=None` selects all columns and `filter=None` leaves the native match-all filter unchanged. `columns` maps to native `selected_fields`, and `filter` to native `row_filter`; supplying an alias and its native keyword together raises `TypeError`. `snapshot_id` selects a retained historical snapshot; omission reads the current snapshot. Native validation/storage errors propagate.

`scan.to_arrow() -> pa.Table` and `scan.to_polars() -> pl.DataFrame` materialize selected rows. `scan.to_arrow_batch_reader() -> pa.RecordBatchReader` first materializes the complete Arrow table; it is not a bounded-memory streaming guarantee.

`current_snapshot()` is delegated to PyIceberg and returns its current snapshot object or `None`; `snapshot.snapshot_id` is the committed ID and `snapshot.parent_snapshot_id` is the predecessor or `None`. Like `current_version_id`, it uses cached metadata. `refresh()` reloads native metadata and returns the native PyIceberg table. These native operations require `push()` and retain PyIceberg error behavior.

```python
table.append_scan(
    row_filter: str | BooleanExpression = ALWAYS_TRUE,
    selected_fields: tuple[str, ...] = ("*",),
    case_sensitive: bool = True,
    start_snapshot_id: int | None = None,
    snapshot_id: int | None = None,
    options: Properties = EMPTY_DICT,
    limit: int | None = None,
)
```

This refreshes metadata and returns an incremental scan. PyIceberg's `BooleanExpression`/`ALWAYS_TRUE` describe a filter/match-all filter; `Properties`/`EMPTY_DICT` are scan options/empty options. `selected_fields` projects columns, `case_sensitive` controls field binding, and `limit=None` means unlimited. `snapshot_id=None` selects the current end snapshot; `start_snapshot_id=None` excludes no baseline files. A supplied baseline must appear in table history, otherwise `AssertionError` is raised.

The result exposes `start_snapshot_id`, `snapshot_id`, `to_arrow()`, and `to_polars()`. This is end-snapshot file selection minus baseline files, not primary-key comparison or row deduplication. Rewritten files may contain previously seen logical rows. Snapshot expiration and missing files can prevent historical/incremental reads; native errors propagate.

## Append result

`AppendResult` is returned by both Iceberg and [Lance](lance.md) appends. Authors normally obtain it from `table.append(...)` and may return it from a node for [Stream](streams.md) consumption.

| Field | Meaning |
| --- | --- |
| `data` | Appended rows as Polars/Arrow data; concrete tables return the cast Arrow table, including enabled provenance. |
| `snapshot_id: int` | Committed Iceberg snapshot ID or Lance version. |
| `table_identity: str \| None` | Concrete tables set their full identifier. |
| `row_model: type[BaseModel] \| None` | Declared Pydantic model, otherwise `None`. |

| Call | Return and errors |
| --- | --- |
| `result.to_polars()` | `pl.DataFrame`; converts Arrow, or returns existing Polars data. |
| `result.to_arrow()` | `pa.Table`; converts Polars/record batches, or returns existing Arrow table. |
| `result.to_dicts()` | `list[dict[str, Any]]`, including all stored columns. |
| `result.to_models()` | List of validated declared model instances; excludes unrelated columns. |
| `result.one()` | One declared model instance; `ValueError` unless exactly one row. |
| `result.one_or_none()` | Declared model instance or `None` for zero rows; `ValueError` for multiple rows. |

All typed methods first require `row_model`, otherwise `TypeError` (even for an empty result). Model validation/conversion errors propagate. Conversions read the result's in-memory data, not storage, and dictionary/model conversion may allocate. The result is not a durable workflow-result store.

Related: [streams](streams.md), [cursors](cursors.md), [workflow primitives](../workflows.md).
