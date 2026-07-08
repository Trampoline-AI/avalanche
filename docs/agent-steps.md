# Agent Steps

`@ava.agent_step` turns a typed, docstring-annotated Python function into an
agent-backed workflow step. Avalanche generates the model-facing signature
internally, executes it through the same DAG machinery as `@ava.step`, unwraps
upstream results into pydantic models by annotation, and stores the returned
model in a table derived from the return annotation.

No DSPy, no `PredictRLM`, no service module, no row schema, and no adapter code
is visible to the author. The dataframe stays at the storage boundary while the
lake keeps every intermediate result durable, queryable, and
provenance-stamped.

## Install

Agent steps use [predict-rlm](https://github.com/Trampoline-AI/predict-rlm)
through the optional `agent` extra:

```bash
uv sync --extra agent
```

Core Avalanche carries no predict-rlm or DSPy dependency; both are imported
lazily the first time an agent step executes.

## Define an agent step

```python
from typing import Annotated

import avalanche as ava
from predict_rlm import File
from pydantic import BaseModel, Field


class RfpAudit(BaseModel):
    requirements: list[str] = Field(description="Things the bidder must satisfy")
    risks: list[str] = Field(description="Potential submission risks")
    required_documents: list[str] = Field(description="Documents to return")


@ava.agent_step(skills=[ava.skills.pdf])
async def audit_rfp(
    documents: Annotated[list[File], ava.Desc("All RFP documents supplied by the issuer")],
) -> RfpAudit:
    """Read the RFP package. Identify submission requirements, risks,
    and required return documents."""
    ...
```

Avalanche reads:

- the function name as the node (and derived table) name;
- parameters as the agent's typed input fields, with `ava.Desc(...)`
  annotations becoming input-field descriptions;
- the docstring as the agent's instructions;
- the return annotation as both the output model and the destination table
  schema. `Field(description=...)` on the model doubles as column
  documentation.

The function body never runs; the `...` body is the convention.

## Agent steps are ordinary steps

Agent steps register through the same node path as `@ava.step`: dependencies,
retries, async execution, `>>` / `&` chaining, table writes, and TUI rendering
behave identically. Plain steps interleave freely for glue logic:

```python
class PreparedInputs(ava.BaseInput):
    rfp_documents: list[ava.File]


@ava.agent_step
async def make_submission_plan(audit: RfpAudit) -> SubmissionPlan:
    """Turn the RFP audit into a practical submission plan."""
    ...


@ava.workflow(input=PreparedInputs)
def proposal_flow():
    audit = audit_rfp(documents=ava.input.rfp_documents)
    plan = make_submission_plan(audit)
    return audit >> plan
```

`ava.input.<field>` is a build-time placeholder resolved against the validated
run input when the node executes. It works on plain steps too.

## Dataflow and cardinality

Each agent step appends its output model to its table the moment it is
produced and returns the typed `ava.AppendResult`. Downstream parameters are
unwrapped by annotation at the step boundary:

- a parameter annotated `Model` receives `result.one()` — exactly one row, or
  a loud error naming the step, parameter, and actual row count;
- a parameter annotated `list[Model]` receives `result.to_models()`;
- a step returning `list[Model]` appends N rows from one call (explode).

Because intra-run dataflow rides `AppendResult` passthrough, each step sees
exactly what this run produced — never rows from a concurrent or previous run
— while every intermediate result stays queryable in the lake with the
standard `_ava_*` provenance columns.

## Configuration

Per-step configuration lives on the decorator; global defaults come from
`ava.configure_agent`. Decorator kwargs win key by key.

```python
ava.configure_agent(
    lm="openai/gpt-5.5",
    sub_lm="gemini/gemini-3.5-flash",
    namespace=ns,  # default namespace for derived tables
)


@ava.agent_step(skills=[ava.skills.pdf], max_iterations=40)
async def audit_rfp(...) -> RfpAudit:
    """..."""
    ...
```

Destination table resolution, in order:

1. `@ava.agent_step(table=ns.audits)` — an explicit table declared from the
   same pydantic model;
2. a table auto-derived in `configure_agent(namespace=...)`, named after the
   step function and created (or bound) on demand;
3. otherwise a configuration error at execution time.

## Migrating existing DSPy signatures

`signature=` bypasses generation so hand-written signatures migrate
incrementally:

```python
@ava.agent_step(table=ns.audits, signature=ExistingAuditSignature)
async def audit_rfp(documents: list[File]) -> RfpAudit:
    """Superseded by ExistingAuditSignature; kept for the workflow contract."""
    ...
```

The signature must expose exactly one output field; its annotation defines the
output and table model.

## Current caveats

- Agent-step tables are created from the return model; schema evolution for
  pydantic-declared tables is not part of this release.
- `Stream` and `Cursor` remain the tools for cross-run incremental-ingestion
  topologies; agent steps are run-scoped by construction.

See [`data-model-api.md`](data-model-api.md#pydantic-model-schemas) for
pydantic-declared tables and typed `AppendResult` access.
