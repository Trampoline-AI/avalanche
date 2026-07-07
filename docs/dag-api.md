# DAG API

Avalanche workflows are Python functions that declare a directed acyclic graph
(DAG) of reusable task functions. The DAG API is intentionally small:

- `@ava.source` ingests or creates data.
- `@ava.step` transforms data inside the flow. `@ava.transform` is an alias.
- `@ava.dest` publishes, exports, or summarizes final results.
- `@ava.workflow` captures task calls and dependencies into a runnable workflow.

Node functions may be regular `def` functions or `async def` coroutines. The
workflow `.run(...)` API is synchronous: Avalanche awaits coroutine node bodies
inside the selected executor before passing results downstream.

The body of a `@ava.workflow` function should stay declarative. It runs when the
workflow is built, not once per row or once per input item. Put business logic in
source, step, and destination functions.

## Minimal workflow

```python
import avalanche as ava

@ava.source
def load_documents() -> list[dict[str, object]]:
    return [{"doc_id": "guide", "tokens": 120}]

@ava.step
def chunk_documents(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"chunk_id": f"{doc['doc_id']}-1", "tokens": doc["tokens"]} for doc in documents]

@ava.dest
def publish_chunks(chunks: list[dict[str, object]]) -> int:
    return len(chunks)

@ava.workflow
def document_flow():
    docs = load_documents()
    chunks = chunk_documents(docs)
    return publish_chunks(chunks)

result = document_flow().run(executor=ava.LocalExecutor())
```

Calling a node inside a workflow returns a deferred `NodeFuture`, not the runtime
value. Passing a `NodeFuture` as an argument creates a dependency and passes the
parent result to the child at execution time.

## Chaining with `>>`

For linear flows, `>>` is a compact way to express “run the next node after this
one.” If the downstream node has no explicit positional arguments, Avalanche
passes the upstream result as its first positional argument.

```python
@ava.workflow
def document_flow():
    return load_documents() >> chunk_documents() >> publish_chunks()
```

This is equivalent to the explicit argument form above.

## Parallel branches with `&`

Use `&` to define branches that can run after the same predecessor and before a
shared successor.

```python
@ava.step
def validate_chunks(chunks):
    return {"valid": True, "count": len(chunks)}

@ava.step
def build_index(chunks):
    return {"index_name": "local-index", "count": len(chunks)}

@ava.step
def summarize_chunks(chunks):
    return {"chunks": len(chunks)}

@ava.dest
def publish_report(validation, index, summary):
    return {"validation": validation, "index": index, "summary": summary}

@ava.workflow
def fanout_flow():
    return (
        load_documents()
        >> chunk_documents()
        >> (validate_chunks() & build_index() & summarize_chunks())
        >> publish_report()
    )
```

Always parenthesize parallel groups. Python parses `>>` before `&`, so
unparenthesized expressions can build a different graph than intended.

```python
# Good: fan-out, then fan-in.
load_documents() >> (validate_chunks() & build_index()) >> publish_report()

# Ambiguous: parsed as (load_documents() >> validate_chunks()) & build_index().
load_documents() >> validate_chunks() & build_index()
```

## Multi-return nodes

Declare `num_returns` when a node returns multiple values that downstream nodes
should address independently. Use indexing inside the workflow to select each
return value.

```python
@ava.source(num_returns=2)
def load_train_test():
    return train_rows(), test_rows()

@ava.step
def train_model(train_rows):
    return {"model": "local-demo", "rows": len(train_rows)}

@ava.step
def evaluate_model(test_rows):
    return {"rows": len(test_rows)}

@ava.workflow
def training_flow():
    datasets = load_train_test()
    model = train_model(datasets[0])
    return evaluate_model(datasets[1])
```

## Runtime providers

Node parameters can use provider defaults such as `ava.Stream`, `ava.Cursor`, and
`ava.Logger`. Providers are resolved at execution time.

```python
@ava.step
def chunk_new_documents(
    _loaded: object = None,
    docs = ava.Stream(ns.document, key="documents_to_chunks"),
    *,
    dest = ns.chunk,
):
    chunks = process_documents(docs)
    return dest.append(chunks)
```

`ava.Stream(table, key=...)` injects one `polars.DataFrame` for the claimed table
snapshot. User code does not call `.read()` on the `Stream` object.

`ava.Cursor(table, key=...)` stores manual checkpoint state in table metadata and
is useful when a task needs custom progress control or coordination across
multiple source tables.

## Run input and context

Workflow builders stay zero-argument. Declare the runtime payload model on the
workflow decorator, then receive input and runtime context in source, step, or
destination functions by type annotation.

```python
import avalanche as ava


class DocumentInput(ava.BaseInput):
    value: int
    document: ava.File
    remote_document: ava.S3File


@ava.source
def load_document(payload: DocumentInput, ctx: ava.RunContext) -> str:
    local_text = payload.document.read_bytes().decode()
    return f"{ctx.run_id}:{payload.value}:{local_text}:{payload.remote_document.uri}"


@ava.workflow(input=DocumentInput)
def document_flow():
    return load_document()
```

Avalanche validates `input` at run start with Pydantic. Unknown fields are
rejected by default. Annotated input and context parameters are injected by type
and do not consume upstream data arguments.

`ava.RunContext` is created by Avalanche for every run. It contains:

- `run_id`: caller/operator-owned run id, or a generated ULID.
- `workflow_name`: the decorated workflow name.
- `executor_type`: `"local"` or `"ray"`.
- `node_id`: current node invocation id, such as `load_document_1`.
- `node_name`: current node function name, such as `load_document`.
- `metadata`: optional caller/platform metadata as `dict[str, object]`.

When a node writes to a default Iceberg or Lance table, Avalanche also uses this
run context to populate row provenance columns such as `_ava_run_id`,
`_ava_workflow_name`, `_ava_node_id`, `_ava_node_name`, and
`_ava_ctx_metadata`. See
[`data-model-api.md`](data-model-api.md#append-scan-and-read) for the table-side
`row_lineage` option.

The workflow author usually does not define a custom context type. If a caller
needs to pass request metadata, put it under `RunContext.metadata`:

Pass runtime values from Python with `.run(input=..., context=...)`:

```python
result = document_flow().run(
    executor=ava.LocalExecutor(),
    run_id="run_123",  # optional caller-owned run identity
    input={
        "value": 41,
        "document": {"name": "doc.txt", "content": b"hello"},
        "remote_document": {"uri": "s3://bucket/large-doc.txt"},
    },
    context={"metadata": {"request_id": "req_123"}},
)
```

Advanced platform integrations may define a shared subclass of `ava.RunContext`
when context fields should be required and typed, for example `org_id`,
`project_id`, or `deployment_id`. Avalanche still owns and overwrites runtime
fields such as `run_id`, `workflow_name`, `executor_type`, `node_id`, and
`node_name`; callers cannot spoof them with `context` payloads.

`ava.File` is for small file payloads carried with a run request. Inline files
are limited to `ava.MAX_INLINE_FILE_BYTES` bytes; use `ava.S3File` above that
limit. gRPC/CLI run requests also limit total inline file bytes to
`ava.MAX_INLINE_REQUEST_BYTES`; use `ava.S3File` when a request needs more file
data. Build one from a path with `ava.File.from_path(path)` or pass `{name,
content, content_type}` in the input payload. `sha256` is computed when omitted
and validated when supplied.

`ava.S3File` is a reference to a large S3-compatible object. It validates `s3://`
URIs and lazy-imports `s3fs` only when `open()` or `read_bytes()` is called.
Install `avalanche-ai[s3]` if your environment does not already include `s3fs`.
Authentication and endpoint options are passed through to `s3fs`:

```python
import os

contents = payload.remote_document.read_bytes(
    key=os.environ["AWS_ACCESS_KEY_ID"],
    secret=os.environ["AWS_SECRET_ACCESS_KEY"],
    client_kwargs={"endpoint_url": "https://s3.example.com"},
)
```

## Execution

Run a workflow by constructing it and calling `.run()` with an executor:

```python
result = document_flow().run(executor=ava.LocalExecutor())
```

`ava.LocalExecutor` runs locally. `ava.RayExecutor` is available when Ray support
is installed and configured.

When a local operator is running, start a discovered workflow over gRPC with
`ava run`. JSON values go through `--input` and `--context`; files and S3
references are attached to top-level input fields without base64-in-JSON:

```bash
uv run ava run document_flow --connect localhost:7433 \
  --input '{"value": 41}' \
  --context '{"metadata": {"request_id": "req_123"}}' \
  --file document=./doc.txt \
  --s3-file remote_document=s3://bucket/large-doc.txt
```

## Current caveats

- Workflow functions should define edges only: avoid loops, conditionals with
  runtime data, network calls, or data processing in the workflow body.
- Task enable/disable modifiers such as `.skip()` or `.only()` are not part of
  the current release-candidate API.
- Operator/TUI discovery imports Python files. Use a specific flow file or a
  clean flow-only directory rather than `--flows .` from the repository root.

See [`examples/complex_dag_pattern.py`](../examples/complex_dag_pattern.py) for a
runnable DAG example.
