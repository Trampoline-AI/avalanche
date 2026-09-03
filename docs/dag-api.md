# DAG API

Avalanche workflows are Python functions that declare a DAG of reusable nodes.

| Decorator | Use |
| --- | --- |
| `@ava.source` | Ingest or create data |
| `@ava.step` | Transform data (`@ava.transform` is an alias) |
| `@ava.dest` | Publish or summarize a result |
| `@ava.workflow` | Build a runnable workflow |
| `@ava.agent_step` / `@ava.agent.step` | Declare an agent-backed step; see [`agent-steps.md`](agent-steps.md) |

Node functions may be `def` or `async def`. Keep workflow bodies declarative:
call nodes and connect their results there; put runtime work in nodes.

## Minimal workflow

```python
import avalanche as ava


@ava.source
def load_documents() -> list[dict[str, object]]:
    return [{"doc_id": "guide", "tokens": 120}]


@ava.step
def chunk_documents(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {"chunk_id": f"{document['doc_id']}-1", "tokens": document["tokens"]}
        for document in documents
    ]


@ava.dest
def publish_chunks(chunks: list[dict[str, object]]) -> int:
    return len(chunks)


@ava.workflow
def document_flow():
    documents = load_documents()
    chunks = chunk_documents(documents)
    return publish_chunks(chunks)


result = document_flow().run(executor=ava.LocalExecutor()).result()
```

Calls inside a workflow return deferred `NodeFuture` values. Passing one to a
node creates a dependency; its result is supplied when the workflow runs.

## Connect nodes

Use normal arguments when names make the graph clear:

```python
@ava.workflow
def document_flow():
    return publish_chunks(chunk_documents(load_documents()))
```

Use `>>` for a linear pipeline. The upstream result becomes the first positional
argument of the downstream node when that node has no explicit positional
arguments.

```python
@ava.workflow
def document_flow():
    return load_documents() >> chunk_documents() >> publish_chunks()
```

Use `&` for branches and parenthesize every parallel group:

```python
@ava.step
def validate_chunks(chunks):
    return {"valid": True, "count": len(chunks)}


@ava.step
def build_index(chunks):
    return {"index_name": "local-index", "count": len(chunks)}


@ava.dest
def publish_report(validation, index):
    return {"validation": validation, "index": index}


@ava.workflow
def fanout_flow():
    return (
        load_documents()
        >> chunk_documents()
        >> (validate_chunks() & build_index())
        >> publish_report()
    )
```

## Multiple outputs

Set `num_returns` when downstream nodes need individual values from a tuple-like
result, then index the returned future.

```python
@ava.source(num_returns=2)
def load_train_test():
    return train_rows(), test_rows()


@ava.workflow
def training_flow():
    train, test = load_train_test()
    model = train_model(train)
    return evaluate_model(model, test)
```

## Runtime providers

Provider defaults are resolved while a node executes. Common providers are
`ava.Stream`, `ava.ModelStream`, `ava.Cursor`, and `ava.Logger`.

```python
@ava.step
def chunk_new_documents(
    _loaded: object = None,
    documents=ava.Stream(ns.documents, key="documents_to_chunks", mode="append_scan"),
    *,
    destination=ns.chunks,
):
    return destination.append(process_documents_to_chunks(documents))
```

| Need | API |
| --- | --- |
| Rows written in this run | `ava.Stream(table)` |
| Incremental table backlog | `ava.Stream(table, key="...", mode="append_scan")` |
| Validated Pydantic rows | `ava.ModelStream.one(table)`, `.one_or_none(table)`, or `.all(table)` |
| Manual table checkpoint | `ava.Cursor(table, key="...")` |

Do not call `.read()` on a `Stream` provider. See
[`data-model-api.md`](data-model-api.md#streams-and-cursors) for table setup and
stream behavior.

## Input and context

Declare a Pydantic `ava.BaseInput` subclass on the workflow. Annotate node
parameters with that type or `ava.RunContext` to receive them.

```python
class DocumentInput(ava.BaseInput):
    value: int
    document: ava.File


@ava.source
def load_document(payload: DocumentInput, ctx: ava.RunContext) -> str:
    return f"{ctx.run_id}:{payload.value}:{payload.document.read_bytes().decode()}"


@ava.workflow(input=DocumentInput)
def document_flow():
    return load_document()


result = document_flow().run(
    executor=ava.LocalExecutor(),
    input={
        "value": 41,
        "document": {"name": "doc.txt", "content": b"hello"},
    },
    context={"metadata": {"request_id": "req_123"}},
).result()
```

`RunContext` includes the run ID, workflow name, executor type, current node
identity, rerun details, and optional caller metadata. `ava.File` carries file
content. Use `ava.File.from_path(path)` when starting a run from Python.

Use `ava.Workspace` for a portable directory input or result:

```python
class ReportInput(ava.BaseInput):
    workspace: ava.Workspace


workspace = ava.Workspace.from_path("./project")
```

Inside a node, `workspace.path` is a local directory. Return the workspace from a
node to pass its changed contents downstream.

### CLI file and workspace inputs

For an operator-managed run, send JSON fields with `--input` and attach top-level
file or workspace fields separately:

```bash
uv run ava run document_file_workflow \
  --connect localhost:7433 \
  --input '{"value": 41}' \
  --file document=./doc.txt

uv run ava run report_workspace \
  --connect localhost:7433 \
  --workspace workspace=./project
```

Repeat `--file` for multiple file fields. A field may appear in only one of
`--input`, `--file`, or `--workspace`.

## Results

A workflow can return JSON-compatible values, a Pydantic model, `ava.File`, or
`ava.Workspace`, including those values nested in models and containers.

```python
from pydantic import BaseModel


class Report(BaseModel):
    summary: str
    files: list[ava.File]


@ava.dest
def build_report() -> Report:
    return Report(
        summary="complete",
        files=[ava.File(name="report.txt", content=b"workflow output")],
    )
```

In embedded mode, `RunHandle.result()` returns the original Python value. For an
operator run, wait for success and retrieve it with `GrpcStateProvider.get_run_result(run_id)`.

Download an operator result with the CLI:

```bash
uv run ava result "$RUN_ID" \
  --connect localhost:7433 \
  --wait \
  --timeout 300 \
  --output-dir ./run-result
```

The output directory must not already exist. The command writes result metadata
and any file or workspace contents beneath it.

## Run and cancel

`.run()` starts a workflow and returns an awaitable `ava.RunHandle` immediately.

```python
run = document_flow().run(executor=ava.LocalExecutor())
print(run.run_id)
result = run.result()

# In async code:
result = await document_flow().run(executor=ava.LocalExecutor())
```

Use `ava.LocalExecutor` for local execution. `ava.RayExecutor` is available when
Ray support is installed. Call `run.cancel()` to request cooperative cancellation.

For task-scoped platform resources, pass `ava.ExecutionServicesSpec`; see
[Execution services](execution-services.md).

## Rerun

Use `ava.Rerun` to run part of an earlier workflow again. Target stable node
slugs; set `slug=` explicitly for nodes that must be rerun by name.

```python
@ava.step(slug="chunk-docs")
def chunk_documents(documents):
    return documents


result = document_flow().run(
    executor=ava.LocalExecutor(),
    run_id="rerun_002",
    rerun=ava.Rerun(run_id="run_001", start=["chunk-docs"], mode="autorun"),
).result()
```

`mode="autorun"` runs each selected node and everything downstream.
`mode="lazy"` runs only the selected nodes. Pass `input=` again when a rerun
needs workflow input; it is not restored from the source run.

## Current caveats

- Workflow bodies define graph edges only; avoid runtime-data conditionals,
  network calls, or data processing there.
- Task enable/disable modifiers such as `.skip()` and `.only()` are not part of
  the current release-candidate API.
- Operator/TUI discovery imports Python files. Pass positional flow targets to
  `ava operator` or `ava dev`, or configure `[tool.avalanche].flow_targets` in
  `pyproject.toml`; use a flow file or dedicated flow directory, not the repository root.

See [`examples/complex_dag_pattern.py`](../examples/complex_dag_pattern.py) for a
runnable DAG example.
