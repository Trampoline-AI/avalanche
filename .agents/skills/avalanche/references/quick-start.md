# Workflow quick start

This example focuses on typed DAG construction and execution modes. Iceberg and
Lance namespace, table, and stream APIs are covered in the storage reference.

## Files

```text
document_flow/
├── __init__.py
├── flow.py
└── schema.py
```

`schema.py`:

```python
from pydantic import BaseModel, Field

import avalanche as ava


class Document(BaseModel):
    document_id: str = Field(min_length=1)
    text: str


class DocumentFlowInput(ava.BaseInput):
    documents: list[Document]


class DocumentBatch(BaseModel):
    documents: list[Document]


class CorpusSummary(BaseModel):
    document_count: int
    character_count: int
```

`flow.py`:

```python
import avalanche as ava

from .schema import CorpusSummary, DocumentBatch, DocumentFlowInput


@ava.source
def prepare_documents(payload: DocumentFlowInput) -> DocumentBatch:
    return DocumentBatch(documents=payload.documents)


@ava.step
def summarize_documents(batch: DocumentBatch) -> CorpusSummary:
    return CorpusSummary(
        document_count=len(batch.documents),
        character_count=sum(len(document.text) for document in batch.documents),
    )


@ava.workflow(input=DocumentFlowInput)
def document_flow():
    return prepare_documents() >> summarize_documents()
```

The workflow builder takes no Python arguments. Avalanche validates the runtime
payload as `DocumentFlowInput` and injects it into the annotated source parameter.

## Embedded execution

Run in the same process with `LocalExecutor`:

```python
from avalanche import LocalExecutor

from document_flow.flow import document_flow
from document_flow.schema import Document, DocumentFlowInput


run = document_flow().run(
    executor=LocalExecutor(),
    input=DocumentFlowInput(
        documents=[Document(document_id="guide", text="Avalanche builds DAGs.")]
    ),
)
print(run.run_id)
result = run.result()
print(result.model_dump())
```

`Workflow.run()` returns immediately with an awaitable `ava.RunHandle`. Use
`.result()` for a synchronous wait. In asynchronous code:

```python
async def run_document_flow(payload: DocumentFlowInput) -> CorpusSummary:
    return await document_flow().run(
        executor=ava.LocalExecutor(),
        input=payload,
    )
```

No operator or gRPC connection is involved. The handle and cached terminal result
belong to this process.

## Operator-managed execution

Install Avalanche and point discovery at a specific flow module, file, or
package so imports remain bounded:

```bash
python -m pip install avalanche-ai
ava operator --flows path/to/document_flow --port 7433
```

The operator imports discovered flow modules and owns run state, logs,
cancellation, schedules, and file watching.

Start the discovered flow from another terminal:

```bash
ava run document_flow --connect localhost:7433 \
  --input '{"documents":[{"document_id":"guide","text":"Avalanche builds DAGs."}]}'
```

For an `ava.File` field on the top-level input, attach bytes without base64 JSON:

```bash
ava run document_flow --connect localhost:7433 \
  --input '{"label":"guide"}' \
  --file document=./guide.pdf
```

`--context` accepts runtime context JSON, for example:

```bash
--context '{"metadata":{"request_id":"req_123"}}'
```

## TUI execution

Install Avalanche, then connect the TUI to the operator:

```bash
python -m pip install avalanche-ai
ava tui --connect localhost:7433
```

The TUI discovers workflows and controls runs over gRPC. It does not import or
execute the flow directly.

For the shortest local operator-plus-TUI path:

```bash
ava dev --flows path/to/document_flow
```


Use mock mode only for UI exploration:

```bash
ava tui
```
