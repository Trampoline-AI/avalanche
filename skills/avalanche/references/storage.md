# Table-backed storage: Iceberg and Lance

Avalanche supports both Iceberg and Lance through the same namespace, table,
append-result, model-stream, dataframe-stream, and provenance contracts.
Backend-specific classes configure storage; workflow node bodies can depend on
the neutral `ava.Namespace` and `ava.Table` interfaces.

## Package layout

Keep schemas and storage declarations outside `flow.py`:

```text
proposal_flow/
├── flow.py
├── namespace.py
├── schema.py
└── util.py
```

Use Pydantic models directly as table schemas; do not create parallel DataFramely
mirror classes.

## Define table schemas

`schema.py`:

```python
from pydantic import BaseModel


class AuditArtifact(BaseModel):
    package_id: str
    requirements: list[str]
    risks: list[str]


class ProposalArtifact(BaseModel):
    package_id: str
    body: str
```

## Define and push a namespace

Both backends use Pydantic models directly as table schemas.

### Iceberg

```python
from pathlib import Path

import avalanche as ava

from .schema import AuditArtifact, ProposalArtifact


class ProposalIcebergNamespace(ava.IcebergNs):
    ns_config = ava.IcebergNsConfig(
        name="proposal",
        base_location=str(Path(".avalanche/proposal") / "warehouse"),
    )

    audits = ava.IcebergTable(schema=AuditArtifact)
    proposals = ava.IcebergTable(schema=ProposalArtifact)


ns = ProposalIcebergNamespace(
    catalog="proposal",
    load_catalog_props={
        "type": "sql",
        "uri": "sqlite:///.avalanche/proposal/catalog.db",
    },
)
ns.push()
```

Iceberg namespaces bind tables through a PyIceberg catalog. Catalog type,
credentials, URI, and warehouse location belong in typed configuration imported
by `namespace.py`.

### Lance

Install the Lance extra in the environment that executes table operations:

```bash
python -m pip install "avalanche-ai[lance]"
```

```python
import avalanche as ava

from .schema import AuditArtifact, ProposalArtifact


class ProposalLanceNamespace(ava.LanceNamespace):
    ns_config = ava.LanceNamespaceConfig(
        name="proposal",
        base_location=".avalanche/proposal/lance",
    )

    audits = ava.LanceTable(schema=AuditArtifact)
    proposals = ava.LanceTable(schema=ProposalArtifact)


ns = ProposalLanceNamespace()
ns.push()
```

Lance namespaces bind each declaration to a Lance dataset under the configured
base location.

For either backend, `ns.push()` creates or binds the namespace and its declared
tables. Common namespace operations include `push()`, `drop()`, and
`list_tables()`. Common table operations include `append()`, `scan()`, `read()`,
`read_models()`, `history()`, and `current_version_id`.

## Persist from a node

Bind a table as a keyword-only default and append a model:

```python
from .namespace import ns
from .schema import AuditArtifact


@ava.agent_step(AuditPackage)
async def audit_package(
    prepared: PreparedPackage,
    *,
    agent: ava.Agent,
    dest: ava.Table = ns.audits,
) -> ava.AppendResult:
    prediction = await agent(package=prepared)
    artifact = AuditArtifact.model_validate(prediction.audit)
    return dest.append(artifact)
```

Appending returns `ava.AppendResult`, which carries both appended data and the
backend snapshot/version id. Returning it preserves the dependency and lets a
downstream stream select the exact live result.

## Consume persisted models

Prefer `ava.ModelStream` for Pydantic-declared tables. It removes framework
provenance columns and validates the selected row before invoking the node:

```python
from .namespace import ns
from .schema import AuditArtifact, ProposalArtifact


@ava.agent_step(DraftProposal)
async def draft_proposal(
    audit: AuditArtifact = ava.ModelStream.one(ns.audits),
    *,
    agent: ava.Agent,
    dest: ava.Table = ns.proposals,
) -> ava.AppendResult:
    prediction = await agent(audit=audit)
    return dest.append(ProposalArtifact.model_validate(prediction.proposal))
```

Connect it in the DAG:

```python
@ava.workflow(input=ProposalInput)
def proposal_flow():
    prepare_inputs() >> audit_package() >> draft_proposal()
```

The `audit` `NodeFuture` selects that exact upstream append while it is live.
`ava.ModelStream.one(ns.audits)` remains the execution-time provider and asserts
the one-row cardinality contract. Use `one_or_none` for an optional row and
`all` for a list:

```python
audit = ava.ModelStream.one(ns.audits)
maybe_audit = ava.ModelStream.one_or_none(ns.audits)
audits = ava.ModelStream.all(ns.audits)
```

Use `ava.Stream(ns.audits)` only when a dataframe-native stage genuinely needs a
Polars `DataFrame`.

## Stream modes

Model and dataframe streams are run-scoped by default: they read rows associated
with the current run through Avalanche row lineage. Use this for ordinary
stage-to-stage durable flow.

Incremental backlog stream:

```python
audits = ava.ModelStream.all(
    ns.audits,
    key="audits_to_proposals",
    mode="append_scan",
)
```

This claims one pending snapshot at a time, oldest first. Use it only for durable
queue-like draining across runs. The `key` is valid only with
`mode="append_scan"`; schedule repeated runs to drain multiple snapshots.

## Provenance

Iceberg and Lance tables add the same framework-owned provenance columns,
including update time, run id, workflow name, node id/name/slug, rerun source,
lineage vector, and context metadata. These fields describe writes and run
lineage rather than inferred entity lineage or primary keys.

Set `row_lineage=False` on either table type when declaring a table without
framework provenance:

```python
iceberg_export = ava.IcebergTable(schema=ExternalExport, row_lineage=False)
lance_export = ava.LanceTable(schema=ExternalExport, row_lineage=False)
```

## Direct reads and backend-specific APIs

The model and dataframe read paths are backend-neutral:

```python
all_audits = ns.audits.read_models()
selected = ns.audits.scan(columns=["package_id", "risks"]).to_polars()
```

Iceberg tables additionally proxy supported PyIceberg operations such as
snapshots, metadata, history, and backend-specific scans. Lance tables expose
Lance history and versions through `history()` and `current_version_id`; Lance
scans support columns, filters, and limits.

## Boundaries and caveats

- Tables may represent intermediate or final artifacts; table scope is part of
  the workflow's data model.
- Keep table declarations in `namespace.py` and model definitions in `schema.py`.
- Return `AppendResult` when a downstream stream must select that exact write.
- Validate model outputs before append.
- Iceberg and Lance share Avalanche's neutral contracts but retain their native
  backend behavior and configuration.
- Lance `append_scan` replays one data-producing version from its direct parent;
  it does not traverse arbitrary version ranges.
- Catalog authentication and object-store configuration are environment-specific.

## Verification

1. Push the configured Iceberg or Lance namespace.
2. Append a Pydantic model and verify a real snapshot/version id is returned.
3. Read the row through `read_models()` and validate its model type.
4. Exercise the same row through `ModelStream` or `Stream` in a workflow.
5. For `append_scan`, verify the backend's documented version-progress behavior.
