# Agent steps: signatures, calls, skills, and tools

Install the standard wheel in the target UV project:

```bash
uv add avalanche-ai
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
Every non-inline signature MUST be defined in a separate `signature.py`, not in
`flow.py`, regardless of size or reuse. For one agent, a root `signature.py` is
sufficient; use per-agent directories for larger flows.


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

## Native evaluations

Use `@ava.agent_step(..., evaluations=ava.Evaluations(...))` for automatic,
observation-only quality judgments. Do not create a downstream classifier node
merely to observe this step, and do not turn native evaluations into workflow
gates, routing, retries, or self-correction.

Keep the **selected evidence** separate from the **question**. Each metric has a
synchronous `state` callable and one ordinary TypeSafe question from the native
classifier format:

```python
audit_evaluations = ava.Evaluations(
    metrics={
        "specific_risks": ava.Metric(
            state=lambda ctx: ctx.output.model_dump(mode="json"),
            question={
                "type": "score",
                "instructions": "How specifically does the audit describe its risks?",
                "criteria": [
                    "Risks are missing or vague",
                    "Some risks explain a concrete problem",
                    "Every stated risk explains a concrete problem",
                ],
            },
        ),
        "actionable": ava.Metric(
            state=lambda ctx: ctx.output.model_dump(mode="json"),
            question={
                "type": "noul",
                "instructions": "Does the audit explain what needs attention?",
            },
        ),
        "checked_evidence": ava.Metric(
            state=lambda ctx: ctx.trace,
            question={
                "type": "noul",
                "instructions": (
                    "Do the recorded agent invocations show that the agent checked "
                    "the supplied evidence before producing its final answer?"
                ),
            },
        ),
    },
    composites={
        "usefulness": lambda results: (
            0.7 * (results.scores["specific_risks"].score / 2)
            + 0.3 * results.nouls["actionable"].noul
        ),
    },
)


@ava.agent_step(AuditPackage, evaluations=audit_evaluations)
async def audit_package(package: PreparedPackage, *, agent: ava.Agent) -> PackageAudit:
    prediction = await agent(package=package)
    return PackageAudit.model_validate(prediction.audit)
```

These selectors reuse the `PackageAudit` return type above; do not introduce
another input/output/context schema for evaluations:

- `ava.EvalContext.inputs` contains bound step arguments by name, including
  defaults, excluding injected services such as `agent`.
- `ctx.output` is the **actual final Python return**, not necessarily the agent's
  raw prediction or the final model call. Select fields directly from existing
  Python/Pydantic objects; convert a whole model with `.model_dump(mode="json")`.
- `ctx.trace` is a JSON-compatible list of terminal events from **every agent
  invocation in the step**, with invocation IDs, exported trace bodies, and
  unavailable-trace errors. It is not just the last invocation.
- Combine relevant inputs and outputs explicitly, for example
  `{"request": ctx.inputs["question"], "answer": ctx.output.answer}` for a step
  whose contract has those fields. Optional `ava.EvalContext` annotations aid
  static checking; no automatic lambda inference is promised.
- Return text, JSON objects, or JSON arrays with finite nested numbers from
  selectors. Convert Python-only values explicitly. Native evaluations do not
  open files, extract content, or evaluate images/media. Paths are not evidence
  of the referenced file's contents.

Full terminal traces may repeat steps under `evidence.events` and repeat each
output as `untruncated_output`. For trace-quality judgments, select the source
and observable action fields needed for the question instead of sending every
copy. Jev can return HTTP 400 `max_tokens_exceeded` for oversized state; Avalanche
exposes the machine code but not the potentially private request body.

Equal selected state with compatible evaluator configuration is batched by
content, not selector identity. The two audit-output metrics above share a
request; the trace metric remains separate. Do not combine different states to
force batching or apply a question-count heuristic. Service limits still apply.
See [question design](usage.md#structure-the-questions-object) for Choice, Noul,
and Score formats; a metric takes one question, not a question mapping.

Composites receive `ava.ClassificationResult`. Use `results.answers[name]`,
`results.choices[name].choice`, `results.nouls[name].noul`, and
`results.scores[name].score`. Choice/Score preserve `.probabilities` and
`.confidence`; Score also preserves `.legend`. Noul is probability of yes,
not a Boolean or separate confidence. An `N`-level Score is in `[0, N - 1]`:
normalize explicitly using `score / (N - 1)`. The three-level rubric above uses
`/ 2`. Synchronous composite functions must return finite numbers in `[0, 1]`.
They make no extra model calls and do not depend on other composites; never
invent `.normalized` or `.probability` aliases.

Set `TYPESAFE_API_KEY` in the operator environment, in addition to credentials
for the agent's provider. `ava.Evaluations(model=..., timeout=...)` overrides
workflow `classifier_defaults`; `None` inherits. Defaults are `jev-latest` and
a 10-second SDK request timeout. Agent `lm`/`sub_lm` settings do not configure
Jev. Declarations validate questions but make no model calls during discovery.

TypeSafe HTTP 401 indicates rejected authentication, not a quality judgment.
Verify `TYPESAFE_API_KEY`, including exported values that override `.env`; restart
the operator after changing credentials and run again. Never print the key.

Automatic evaluations run only in operator mode after successful step returns.
Downstream execution and workflow result delivery never wait for them.
Selectors, invalid state, Jev failures, and invalid composites become independent
evaluation errors, never fallback scores or workflow failures. Failed steps
do not schedule evaluations. Embedded Python `.run()` reports **not evaluated**
and starts no automatic evaluation worker.

Before execution, graph nodes show an **Evaluations** badge with the metric count.
Compact nodes show only its icon and count. The current-definition inspector's
**Evals** tab shows
named metrics, types, expandable criteria, composite names, and the effective Jev
model/timeout. **Agent definition** retains instructions, agent inputs/outputs,
models, and resources; **Step definition** contains only the step interface card.
Historical run graphs use their captured declarations, not later source edits.
Metadata discovery never executes evidence selectors or composite functions.

Each metric has a collapsed **Input** section listing statically visible source
paths such as `input.packet`, `output.summary`, `output`, and `trace`. Path roots
refer to the evaluated step's arguments, result, and agent trace; opaque selectors
show `custom: qualified_name`. This is selection metadata, not the serialized
state sent to Jev or execution of the selector. Completed
composites appear beside run-node evaluation badges and in the agent sidebar
header: one uses `label: 85.6%`, multiple use up to three one-decimal percentages
separated by colons in declaration order. Percentages blend red at 0%, yellow at
50%, and the success green (`#22c55e`) at 100%; labels have matching node/sidebar
colors. Compact nodes omit composite
names and match percentages and pill counts to the duration's size. Historical
views use their captured selector metadata.

The selected run agent step's **Evaluations** tab shows one
pending/completed/failed result directly. Composites precede Metrics as equally
styled top-level sections without boxes. Choice options list their percentages
once, with only the winner bold and turquoise; other choice percentages,
score-level probabilities, and confidence stay neutral. Noul values and bars
keep the value gradient. Composite tab values match the node/header percentage
format and gradient. Reruns have separate results on their own run snapshots. Work survives
coordinator completion, but records live only in the running operator's memory
and disappear on restart.
Do not promise durable recovery or claim workflow success proves evaluation
success. Verify real judgments only with actual credentials; controlled SDK
fixture responses can verify UI behavior but are not live Jev evidence.

Repository example: `examples/evaluations_workflow.py`, run with
`uv run ava dev examples/evaluations_workflow.py` from the repository root after
setting `OPENAI_API_KEY` and `TYPESAFE_API_KEY`. Click **Run** without supplying
input. It generates synthetic incident evidence, uses a real agent to prepare a
cited on-call handoff, and renders a Markdown brief. The agent step demonstrates
all three question types, shared-state batching, trace selection, and a normalized
composite; no customer communication is sent. The
[illustrated reference](https://github.com/Trampoline-AI/avalanche/blob/main/docs/agent-steps.md#native-evaluations)
also documents browser results/errors and the record APIs.

## Verification

- Smoke-call the real decorated workflow with the intended LM credentials.
- Exercise every input/output field and model validation path.
- Exercise host tools against real bounded fixtures, not placeholder returns.
- For file-modifying agents, inspect the produced artifact using the appropriate
  PredictRLM skill's required verification procedure.
- Treat the raw prediction as untrusted until the step has validated it into the
  declared Pydantic output model.
