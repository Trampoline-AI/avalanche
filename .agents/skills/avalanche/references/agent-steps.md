# Agent steps: signatures, calls, skills, and tools

Install the standard wheel using the environment's package manager:

```bash
python -m pip install avalanche-ai
```

`@ava.agent_step` wraps the PredictRLM runtime and injects a callable
`ava.Agent` into the step body. The two public spellings are equivalent:

```python
ava.agent_step
ava.agent.step
```

## Design one agent step with the PredictRLM skill

[rlm.md](rlm.md) is a vendored copy of the original `rlm` skill from the
PredictRLM package repository. It describes how to design one callable RLM:
validate that the task fits an RLM, define its inputs and outputs, research
feasibility, select reusable capabilities, and write its signature strategy.

When the user is designing a single agent step directly, begin with Step 1 of
that reference: ask what outcome the step must achieve, what information goes
in, what the caller expects back, and how success will be judged. When the step
comes from an already aligned Avalanche workflow, confirm those same facts from
the stage contract before choosing the RLM architecture.

Before applying that design process, state the proposed agent step as one
logical unit of work: one specific task with one cohesive typed result. The
result may have multiple related output fields or files when they jointly
complete that task. The signature strategy may contain several actions needed
to finish the task, but the agent step itself must not be a list of independent
tasks or deliverables.

Define the step's source authority, the decisions inherited from upstream
stages, its completion condition, and explicit non-goals. It must consume prior
results rather than rediscovering or re-deciding them, and it must not perform
work assigned to later stages. If portions could be accepted, retried, reused,
or changed independently, they belong in separate Avalanche nodes.

Load that reference when deciding how one `@ava.agent_step` should work. It is
not the design process for the complete Avalanche workflow or DAG; the main
Avalanche skill owns stage decomposition, topology, deterministic nodes,
persistence, and execution. Once the single RLM step is designed through Steps
1–6 of the original skill, return here to implement its Avalanche signature,
decorator, injected `ava.Agent` call, validation, and return value.

## Typed signature: the default

Use a class for substantial, shared, or independently tested contracts.

`agents/package_audit/schema.py`:

```python
from pydantic import BaseModel, Field


class Requirement(BaseModel):
    identifier: str
    text: str


class PackageAudit(BaseModel):
    requirements: list[Requirement]
    risks: list[str] = Field(default_factory=list)
```

`agents/package_audit/signature.py`:

```python
import avalanche as ava

from ...schema import PreparedPackage
from .schema import PackageAudit


class AuditPackage(ava.Signature):
    """Audit the package into traceable requirements and submission risks.

    Read every supplied document. Preserve issuer identifiers verbatim. Record
    an explicit risk when a requirement is ambiguous or unsupported rather than
    inventing an answer.
    """

    package: PreparedPackage = ava.InputField(
        desc="Validated package and file inventory for this run."
    )
    audit: PackageAudit = ava.OutputField(
        desc="Complete requirements and risks derived from the package."
    )
```

## Signature instructions are docstrings

The class docstring is the signature's instruction text consumed by DSPy and
PredictRLM. It is not ordinary explanatory commentary. Put the complete
agent-step-specific task, strategy, constraints, and quality bar there.
`ava.InputField(desc=...)` and `ava.OutputField(desc=...)` describe individual
fields; they do not replace the signature docstring.

Do not create a one-off Skill to hold instructions that belong only to this
signature. For the inline factory form, the second argument to
`ava.agent.Signature(fields, instructions)` supplies the instruction text because
there is no class docstring.

`flow.py`:

```python
@ava.agent_step(
    AuditPackage,
    skills=[ava.agent.skills.pdf],
    tools=[lookup_policy],
    max_iterations=30,
)
async def audit_package(
    package: PreparedPackage,
    *,
    agent: ava.Agent,
) -> PackageAudit:
    prediction = await agent(package=package)
    return PackageAudit.model_validate(prediction.audit)
```

Rules:

- Each `ava.InputField` name must be supplied exactly once to `await agent(...)`.
- Read prediction fields by their exact `ava.OutputField` names.
- Keep the `agent` parameter keyword-only, without a default, annotated
  `ava.Agent`.
- Never pass `agent` at a DAG call site; Avalanche injects it at execution.
- The body owns all selection, mapping, validation, composition, and persistence.
- An agent-step body may be `def` or `async def`, but model calls are awaitable,
  so normal bodies are asynchronous.

## Inline signature for a small local contract

Construct the signature directly in the agent-step decorator when creating a
directory and class would be more ceremony than clarity:

```python
@ava.agent_step(
    ava.agent.Signature(
        "question: str, context: str -> answer: str, citations: list[str]",
        "Answer only from context and cite the supporting passages.",
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

Use the typed class form as soon as the contract has nested models, substantial
strategy instructions, reuse, or its own tests.

## PredictRLM skills

Avalanche creates and invokes the PredictRLM runtime behind `ava.Agent`. Flow
authors configure its capabilities with `Skill` objects passed through
`skills=`. Skills are passed through unchanged, and built-ins are lazy
re-exports:

```python
ava.agent.skills.pdf
ava.agent.skills.spreadsheet
ava.agent.skills.docx
```

Define custom skills in `skills.py` when the knowledge or capability is reused by
multiple agent steps:

```python
import avalanche as ava


evidence_grounding_skill = ava.agent.Skill(
    name="evidence-grounding",
    instructions="""Ground claims in supplied evidence.

Separate sourced facts from proposals, preserve source identifiers, and mark
unsupported claims explicitly. Apply this procedure whenever an agent analyzes
or drafts from an evidence package.
""",
)
```

Pass the same reusable Skill to each agent step that needs the capability:

```python
@ava.agent_step(AuditPackage, skills=[evidence_grounding_skill])
async def audit_package(request: AuditRequest, *, agent: ava.Agent):
    ...


@ava.agent_step(DraftProposal, skills=[evidence_grounding_skill])
async def draft_proposal(request: ProposalRequest, *, agent: ava.Agent):
    ...
```

A PredictRLM `Skill` can provide:

- `instructions`: domain and procedural guidance injected into the RLM;
- `packages`: PyPI packages installed in the WASM sandbox;
- `modules`: Python files mounted as importable sandbox modules;
- `tools`: host-side functions bundled with the skill.

Custom skill configuration:

- Create a custom Skill for reusable knowledge or capability, not for one
  signature's task instructions.
- Use only pure-Python wheels or packages available in Pyodide. Native extensions
  require an Emscripten build and otherwise do not run in the sandbox.
- Use host-side tools for capabilities that require native binaries, subprocesses,
  unrestricted filesystem access, databases, or external services.
- Configure explicit allowed domains when sandbox code needs network access.
- Use `File` inputs for large documents or images so content can be inspected on
  demand instead of injected as one large prompt.
- Keep Skill instructions general enough to apply across agent steps. Keep each
  agent step's task-specific instructions in its signature docstring, and keep
  host access in tools.
- Test package installation, mounted-module imports, tool calls, and output
  validation in the actual PredictRLM runtime.

## Tools

Avalanche's decorator takes a sequence of callable tools:

```python
@ava.agent_step(
    DraftProposal,
    tools=[search_requirements, fetch_approved_fact],
)
async def draft_proposal(request: ProposalRequest, *, agent: ava.Agent):
    ...
```

Put reusable tool functions in `util.py` or a dedicated `tools/` package. Each
tool must have:

- a unique, stable `__name__` (no lambdas);
- typed inputs and a serializable output;
- a docstring precise enough for the model to choose it correctly;
- host-side validation and bounded access to files, APIs, or databases.

Use a Skill when the model needs instructions or sandbox packages/modules. Use a
direct tool when a self-describing host function is the capability. A Skill may
bundle closely related tools.

## Files

Use `ava.agent.File` for large documents and images so the agent can inspect
content on demand rather than receiving a huge text blob. Built-in document
skills teach the agent how to read or modify those files. `ava.File` is the
Avalanche run-input transport type, while `ava.agent.File` is the agent contract
type. Convert between them explicitly in the agent-step body or a helper.

## Runtime defaults

Shared execution policy belongs on the workflow:

```python
@ava.workflow(
    input=ProposalInput,
    agent_defaults={
        "lm": "openai/gpt-5.5",
        "sub_lm": "gemini/gemini-3.5-flash",
        "max_iterations": 30,
        "verbose": False,
    },
)
def proposal_flow():
    ...
```

Override exceptional steps on their decorator:

```python
@ava.agent_step(AuditPackage, max_iterations=60)
async def audit_package(package: PreparedPackage, *, agent: ava.Agent):
    ...
```

Resolution order is:

```text
agent-step runtime kwargs > workflow agent_defaults > PredictRLM defaults
```

Workflow defaults cannot define `signature`, `skills`, or `tools`; those are
capabilities of a specific agent step.

## Verification

- Smoke-call the real decorated workflow with the intended LM credentials.
- Exercise every input/output field and model validation path.
- Exercise host tools against real bounded fixtures, not placeholder returns.
- For file-modifying agents, inspect the produced artifact using the appropriate
  PredictRLM skill's required verification procedure.
- Treat the raw prediction as untrusted until the step has validated it into the
  declared Pydantic output model.
