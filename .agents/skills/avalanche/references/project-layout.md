# Project layout and flow organization

`flow.py` is the readable index of the DAG, not a general implementation module.
It contains imports, decorated node definitions, and the workflow declarations
at the end. A compact inline signature belongs inside its `@ava.agent_step`
decorator rather than in a standalone module variable. Schemas carry contracts;
`util.py` carries every helper; agent directories carry large model contracts.

## Small flow

Use this when there are only a few nodes and compact agent signatures:

```text
my_flow/
├── __init__.py
├── flow.py       # node definitions, then workflow declaration
├── schema.py     # every Pydantic input, intermediate, and output model
├── util.py       # every helper function
└── skills.py     # reusable knowledge shared by multiple agent steps
```

Include `skills.py` only when a custom Skill is shared by multiple agent steps.

Construct a compact inline signature directly in the agent-step decorator:

```python
@ava.agent_step(
    ava.agent.Signature(
        "question: str, context: str -> answer: str, citations: list[str]",
        "Answer only from the supplied context and cite supporting passages.",
    )
)
async def answer_question(
    request: QuestionRequest,
    *,
    agent: ava.Agent,
) -> Answer:
    prediction = await agent(
        question=request.question,
        context=request.context,
    )
    return Answer(answer=prediction.answer, citations=prediction.citations)
```

The signature may have scalar fields because DSPy owns that call boundary; the
workflow node still receives and returns Pydantic models.

## Larger flow with several agents

Create a directory per substantial agent contract:

```text
proposal_flow/
├── __init__.py
├── flow.py
├── schema.py                 # shared workflow inputs and inter-stage models
├── util.py                   # mapping, validation, conversion, and file helpers
├── skills.py                 # reusable Skills shared across agent steps
├── config.py                 # optional environment/config loading
├── namespace.py              # Iceberg or Lance namespaces and tables
└── agents/
    ├── __init__.py
    ├── package_audit/
    │   ├── __init__.py
    │   ├── schema.py         # models private to this agent contract
    │   └── signature.py      # one typed ava.Signature
    ├── submission_plan/
    │   ├── __init__.py
    │   ├── schema.py
    │   └── signature.py
    └── proposal_draft/
        ├── __init__.py
        ├── schema.py
        └── signature.py
```

Stage-prefixed directories such as `stage1_package_audit/` are appropriate when
sequence is domain-significant. Domain names without numeric prefixes are better
when the DAG topology already communicates ordering.

## `flow.py` order

Use this strict top-to-bottom order:

1. imports from `schema.py`, `util.py`, agent modules, skills, and namespaces;
2. decorated `@ava.source`, `@ava.step`, `@ava.agent_step`, and `@ava.dest`
   definitions, with compact signatures constructed inside their agent-step
   decorators;
3. `@ava.workflow` declarations as the final section.

For example:

```python
import avalanche as ava

from .agents.package_audit.signature import AuditPackage
from .agents.proposal_draft.signature import DraftProposal
from .schema import Audit, Draft, PreparedInputs, ProposalInput
from .util import normalize_documents


@ava.source
def prepare_inputs(payload: ProposalInput) -> PreparedInputs:
    return normalize_documents(payload)


@ava.agent_step(AuditPackage)
async def audit_package(
    prepared: PreparedInputs,
    *,
    agent: ava.Agent,
) -> Audit:
    prediction = await agent(prepared=prepared)
    return prediction.audit


@ava.agent_step(DraftProposal)
async def draft_proposal(
    prepared: PreparedInputs,
    audit: Audit,
    *,
    agent: ava.Agent,
) -> Draft:
    prediction = await agent(prepared=prepared, audit=audit)
    return prediction.draft


@ava.workflow(input=ProposalInput)
def proposal_flow():
    (
        (s0 := prepare_inputs())
        >> (s1 := audit_package(s0))
        >> draft_proposal(s0, s1)
    )
```

Nothing follows the workflow declarations. Do not define models, signature
classes, config loaders, namespace constructors, runners, or undecorated helper
functions in `flow.py`. Put every helper in `util.py`, including
`_normalize_documents`, `_one_from_stream`, validators, formatters, converters,
and filesystem helpers.

## Schema ownership

- `ava.BaseInput`: exactly one runtime payload model for a workflow.
- Root `schema.py`: types shared by multiple nodes or agents.
- Agent-local `schema.py`: types meaningful only to that signature.
- `signature.py`: the `ava.Signature` class. Its class docstring is the complete
  instruction for that agent step; the file contains no skill configuration,
  tools, persistence, or workflow logic.
- Do not mirror a Pydantic model with a second DataFramely model. Avalanche tables
  can use the Pydantic class directly.
- Prefer nested models and lists over JSON strings. Use `Annotated[..., ava.Json]`
  only for genuinely heterogeneous content that cannot map to a typed model.

## Flow boundaries

A node deserves its own DAG boundary when it is independently reusable, needs a
separate retry/rerun boundary, performs substantial I/O, fans out separately, or
produces a durable artifact. Keep trivial mapping and output composition inside
the relevant node body, delegated to helpers in `util.py` when it would distract
from the flow.

The workflow builder itself must remain edge-only. Runtime values are
`NodeFuture` objects there, so do not inspect them, iterate them, branch on them,
or perform I/O with them.

## Anti-patterns

- One monolithic function that calls every stage outside the DAG.
- Any undecorated helper, model, configuration, namespace, or runner in `flow.py`.
- Untyped `dict[str, object]` payloads crossing node boundaries.
- Agent directories for two-line inline signatures.
- Giant signature classes embedded in `flow.py`.
- Skills or tools declared as signature metadata.
- Unparenthesized parallel expressions such as `a() >> b() & c()`.
