# Project layout and flow organization

`flow.py` is the readable index of the DAG, not a general implementation module.
It contains imports, decorated node definitions, and the workflow declarations
at the end, including inline `ava.agent.step(...)` graph calls. Construct a
compact signature inside that call or either agent-step decorator, never as a
standalone module variable. Every named signature MUST live in a separate
`signature.py`, even if small or used once. Schemas carry models; `util.py`
carries every helper; agent directories carry large model contracts.
Workflows run through the operator, never standalone runner scripts,
`main()`/`__main__` blocks, or direct `.run()` entry points.

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
A flow with a named signature also has `signature.py` beside `flow.py`. Larger
flows may put it under `agents/<agent_name>/signature.py`; the separation is
required regardless of signature size.

Construct a compact signature directly in the workflow for a direct invocation:

```python
@ava.workflow
def answer_flow():
    return (
        (load_question() & load_context())
        >> ava.agent.step(
            ava.agent.Signature(
                "question: str, context: str -> answer: str, citations: list[str]",
                "Answer only from the supplied context and cite supporting passages.",
            )
        )
        >> publish_answer()
    )
```

Inputs and outputs follow signature order: `publish_answer` receives `answer`,
then `citations`. Put structured Pydantic payload models in `schema.py`.

Keep a decorated agent body when it adds meaningful preparation, mapping,
batching, custom validation or transforms, composition, or persistence. Such
helpers still belong in `util.py`, and models still belong in `schema.py`.

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
2. decorated `@ava.source`, `@ava.step`, `@ava.agent_step` or
   `@ava.agent.step` functions, and `@ava.dest` definitions, with compact signatures
   constructed inside their decorators;
3. `@ava.workflow` declarations as the final section, including inline
   `ava.agent.step(Signature, ...)` nodes and their compact signature factories.

For example:

```python
import avalanche as ava

from .agents.package_audit.signature import AuditPackage
from .schema import PreparedPackage, ProposalInput
from .util import normalize_documents


@ava.source
def prepare_inputs(payload: ProposalInput) -> PreparedPackage:
    return normalize_documents(payload)


@ava.workflow(input=ProposalInput)
def proposal_flow():
    return prepare_inputs() >> ava.agent.step(AuditPackage)
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

Workflow bodies only declare the graph. Use direct returned `>>` chains by
default; agents run later. Do not inspect future values, branch on them, perform
I/O, or nest agent decorators in the workflow body.

## Anti-patterns

- One monolithic function that calls every stage outside the DAG.
- Any undecorated helper, model, configuration, namespace, or runner in `flow.py`.
- Untyped `dict[str, object]` payloads crossing node boundaries.
- Agent directories for two-line inline signatures.
- Any named signature defined outside its separate `signature.py`.
- Skills or tools declared as signature metadata.
- A body whose only purpose is one direct agent call and output-field forwarding.
- Unparenthesized parallel expressions such as `a() >> b() & c()`.
