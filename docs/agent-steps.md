# Agent steps

For the author-facing decorator parameters, call signature, configuration, and
errors, see the [agent API reference](api/agents.md).
See the [package API index](api/README.md) for other interfaces.

Use inline `ava.agent.step(...)` for one direct agent call. Use a decorated agent
function when you need custom preparation, transformation, or persistence.
Both run on [PredictRLM](https://github.com/Trampoline-AI/predict-rlm); model calls
happen during execution, not workflow construction.

## Inline typed extraction

```python
import avalanche as ava
from pydantic import BaseModel, Field


class KeyItem(BaseModel):
    title: str = Field(min_length=1)
    page: int = Field(ge=1)


class KeyItems(BaseModel):
    items: list[KeyItem] = Field(min_length=1)


class ExtractItems(ava.Signature):
    """Extract the document's key items, citing their page numbers."""

    file: ava.File = ava.InputField(desc="PDF to read.")
    key_items: KeyItems = ava.OutputField(desc="Items grounded in the PDF.")


@ava.source
def load_pdf() -> ava.File:
    with open("brief.pdf", "rb") as pdf:
        return ava.File(name="brief.pdf", content=pdf.read())


@ava.dest
def publish(items: KeyItems) -> KeyItems:
    return items


@ava.workflow(agent_defaults={"runtime": ava.runtimes.PredictRLM(lm="openai/gpt-5.5")})
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(
            ExtractItems,
            runtime=ava.runtimes.PredictRLM(skills=[ava.runtimes.predict_rlm.skills.pdf]),
        )
        >> publish()
    )
```

Inline agent steps bind upstream values in signature input order and return
validated signature outputs: one field becomes its value, multiple fields become
an ordered tuple. Here `publish` receives `KeyItems`, not a raw prediction.
A single list- or tuple-valued field stays one downstream argument, including
when the collection is empty. Multiple fields occupy separate arguments in
declaration order.

## Typed signature class

Use `@ava.agent_step` (or `@ava.agent.step` outside a workflow) when the function
does more than call the agent. Here it also writes the result to a table:

Define inputs and outputs with `ava.Signature`, `ava.InputField`, and
`ava.OutputField`, following [DSPy's Signature API](https://dspy.ai/api/signatures/Signature/).

```python
import avalanche as ava
from predict_rlm import File
from pydantic import BaseModel


class RfpAudit(BaseModel):
    requirements: list[str]
    risks: list[str]


class AuditRfpSig(ava.Signature):
    """Read the RFP and identify requirements and submission risks."""

    documents: list[File] = ava.InputField(
        desc="All RFP documents supplied by the issuer."
    )
    audit: RfpAudit = ava.OutputField(
        desc="Structured RFP requirements and risks."
    )


# `ns.audit_results` is this belt's model-declared audit table.
@ava.agent_step(
    AuditRfpSig,
    runtime=ava.runtimes.PredictRLM(skills=[ava.runtimes.predict_rlm.skills.pdf]),
)
async def audit_rfp(
    documents: list[File],
    *,
    agent: ava.Agent,
    # The table binding remains explicit at the step declaration boundary.
    dest: ava.Table = ns.audit_results,
) -> ava.AppendResult:
    result = await agent(documents=documents)
    return dest.append(result.outputs.audit)
```

The injected `agent` parameter is required and keyword-only. It is never passed
at a workflow callsite:

```python
@ava.workflow(input=PreparedInputs)
def proposal_flow():
    return audit_rfp(documents=ava.input.rfp_documents)
```

## Inline string signature

For a small signature, write it directly in the agent call:

```python
@ava.workflow
def answer_flow():
    return (
        (load_question() & load_context())
        >> ava.agent.step(
            ava.Signature(
                "question: str, context: str -> answer: str, citations: list[str]",
                "Answer from the supplied context and cite supporting passages.",
            )
        )
        >> publish_answer()
    )
```

`publish_answer` receives `answer` and `citations` in that order.

## Agent results and multiple outputs

In a decorated function, `await agent(...)` returns an `ava.agent.AgentResult`
with two fields:

- `result.outputs` holds every output field of the signature, already validated
  against its declared type. A runtime that returns a missing or wrongly typed
  field fails the call with `ava.agent.AgentOutputError`; values are never
  coerced. Inline agent steps validate through the same path.
- `result.trace` is the invocation's `ava.agent.AgentTrace`: a header that every
  runtime fills the same way (status, model, duration, token usage, turn and
  tool-call counts) plus its turns.

The function chooses what to return or save. Neither form saves outputs automatically.

```python
class DraftArtifactsSig(ava.Signature):
    """Render proposal artifacts from an approved plan."""

    plan: ProposalPlan = ava.InputField()
    proposal: str = ava.OutputField()
    compliance_matrix: str = ava.OutputField()


@ava.agent_step(DraftArtifactsSig)
async def render_artifacts(
    plan: ProposalPlan,
    *,
    agent: ava.Agent,
    dest: ava.Table = ns.draft_artifacts,
) -> ava.AppendResult:
    outputs = (await agent(plan=plan)).outputs
    return dest.append(
        DraftArtifacts(
            proposal=outputs.proposal,
            compliance_matrix=outputs.compliance_matrix,
        )
    )
```


## Skills and tools

Pass `tools=` and `skills=` on the agent call or decorator, not on the signature.
Tools are ordinary Python functions with unique names. Skills are
`ava.agent.Skill` objects in the [Agent Skills](https://agentskills.io/specification)
format: a name, a description that says when to use the skill, Markdown
instructions, and optional files the instructions refer to. The agent sees each
skill's name and description and reads the rest only when it needs it. Skills hold
no functions; a skill that needs one documents it, and the step passes it in `tools=`.

Load an existing skill folder, one with a `SKILL.md` file, or build a skill in code:

```python
report_style = ava.agent.Skill.from_dir("skills/report-style")

glossary = ava.agent.Skill(
    name="contract-glossary",
    description="Defines contract terms. Use when a contract term is unclear.",
    instructions="Look the term up in references/glossary.md before answering.",
    files={"references/glossary.md": Path("docs/glossary.md")},
)


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.ClaudeAgent(model="claude-haiku-5-5"),
    tools=[search_contract_repository],
    skills=[report_style, glossary],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    return (await agent(document=document)).outputs.summary
```

`ava.runtimes.ClaudeAgent` and `ava.runtimes.OpenAIAgent` run these skills;
OpenAIAgent's model runs their scripts only with its `"shell"` sandbox tool. PredictRLM does not
support them yet: a PredictRLM step with `skills=` fails before the agent starts. PredictRLM has
its own skills, which bundle instructions with sandbox packages, modules and
tools; pass them on its runtime. The built-in ones are
`ava.runtimes.predict_rlm.skills.pdf`, `.docx` and `.spreadsheet`:

```python
@ava.workflow
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(
            ExtractItems,
            runtime=ava.runtimes.PredictRLM(skills=[ava.runtimes.predict_rlm.skills.pdf]),
            tools=[search_contract_repository],
        )
        >> publish()
    )
```

## Runtime configuration

An agent step's settings live on its runtime, `ava.runtimes.PredictRLM(...)`.
Set shared settings with a workflow-default runtime; a step's runtime is merged
over it field by field, so a step sets only what differs:

```python
@ava.workflow(
    agent_defaults={
        "runtime": ava.runtimes.PredictRLM(lm="openai/gpt-5.5", max_iterations=30)
    },
)
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(
            ExtractItems,
            runtime=ava.runtimes.PredictRLM(
                skills=[ava.runtimes.predict_rlm.skills.pdf], max_iterations=60
            ),
        )
        >> publish()
    )
```

The extraction step above runs with `lm="openai/gpt-5.5"`, the PDF skill and 60
iterations. The same `runtime=` works on agent decorators. Set `verbose=True` on
the runtime to show the PredictRLM trace. Set signatures and tools on each step.
Passing `lm=` or another PredictRLM setting, or a PredictRLM skill, directly to
the step raises `TypeError` naming the `runtime=` replacement.

### Claude agent steps

`ava.runtimes.ClaudeAgent` runs a step with Claude through the Claude Agent SDK
instead of PredictRLM. Install the `claude` extra, which brings the Claude Code
CLI with it, pick a model, and use the step exactly as before:

```python
@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.ClaudeAgent(model="claude-haiku-5-5", max_turns=8),
    tools=[lookup_policy],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    return (await agent(document=document)).outputs.summary
```

The CLI signs in with the first of `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (a Claude subscription), and an
existing `claude` login. A workflow default and a step runtime of different kinds do
not merge: a `ClaudeAgent` step ignores a PredictRLM workflow default. See
[runtimes](api/agents.md#runtimes) for every setting, for file and workspace
inputs, and for how Claude runs a step's skills.

### OpenAI agent steps

`ava.runtimes.OpenAIAgent` runs a step with an OpenAI model through the OpenAI
Agents SDK. Install the `openai` extra, set `OPENAI_API_KEY` where the step runs,
pick a model, and use the step exactly as before:

```python
@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.OpenAIAgent(model="gpt-5.4-mini", max_turns=8),
    tools=[lookup_policy],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    return (await agent(document=document)).outputs.summary
```

By default the model calls the step's tools and nothing else: `ava.File` inputs
are attached to its message, `ava.File` outputs are text files it answers with,
and `ava.Workspace` fields are rejected. With
`builtin_tools=["filesystem", "shell"]`, it edits files and runs commands in the
step's `ava.Workspace` input, in place, through the SDK's local sandbox, which
does not confine commands on Linux. See [runtimes](api/agents.md#openaiagent) for
every setting, and the [OpenAIAgent runtime](runtimes/openai-agent.md#sandbox-tools)
page for the sandbox.

## Native evaluations

Attach observation-only quality judgments to an agent with `evaluations=`, on either
a decorated agent step or an inline `ava.agent.step(...)` call.
Keep each metric's **evidence selector** separate from its **question**: `state` is a
synchronous Python callable; `question` is one existing TypeSafe Noul, Score,
or Choice question, using the same format as
[`classifier_step(questions=...)`](classifier-steps.md).

For a text-review agent, select its existing signature fields:

```python
class Review(BaseModel):
    summary: str


class ReviewSignature(ava.Signature):
    """Summarize the supplied document without adding unsupported claims."""

    document: str = ava.InputField()
    review: Review = ava.OutputField()


review_evaluations = ava.Evaluations(
    metrics={
        "clarity": ava.Metric(
            state=lambda ctx: ctx.output.review.summary,
            question={
                "type": "score",
                "instructions": "How understandable is this summary?",
                "criteria": ["Confusing", "Mostly clear", "Clear throughout"],
            },
        ),
        "concise": ava.Metric(
            state=lambda ctx: ctx.output.review.summary,
            question={
                "type": "noul",
                "instructions": "Is the summary free of unnecessary repetition?",
            },
        ),
        "grounding": ava.Metric(
            state=lambda ctx: {
                "document": ctx.inputs.document,
                "summary": ctx.output.review.summary,
            },
            question={
                "type": "choice",
                "instructions": "How does the summary relate to the document?",
                "criteria": {
                    "supported": "All summary claims are supported by the document.",
                    "unsupported": "At least one claim is unsupported or contradicted.",
                },
            },
        ),
        "checked_evidence": ava.Metric(
            state=lambda ctx: [turn.model_dump(mode="json") for turn in ctx.trace.turns],
            question={
                "type": "noul",
                "instructions": (
                    "Do the recorded agent actions show that the agent checked "
                    "the supplied evidence before producing its final answer?"
                ),
            },
        ),
    },
    composites={
        "readability": lambda results: (
            0.7 * (results.scores["clarity"].score / 2)
            + 0.3 * results.nouls["concise"].noul
        ),
    },
)


@ava.agent_step(ReviewSignature, evaluations=review_evaluations)
async def review_document(document: str, *, agent: ava.Agent) -> Review:
    return (await agent(document=document)).outputs.review
```

An inline agent takes the same declaration:

```python
@ava.workflow
def review_flow():
    return ava.agent.step(
        ReviewSignature,
        inputs={"document": "Quarterly revenue rose 4% on higher renewals."},
        evaluations=review_evaluations,
    )
```

`Evaluations` is a frozen Pydantic model. Its `metrics` and `composites` mappings
are owned, read-only snapshots; changing the dictionaries passed to the constructor
does not change the declaration. Invalid names, metric types, non-callable composites,
model/timeout settings, and unknown fields raise `pydantic.ValidationError` during
construction. Async selectors/composites are still rejected with `EvaluationError`.
This validates the declaration only: selectors and composites do not run until
evaluation, and their runtime failures remain separate from workflow execution.

### Select existing execution evidence

Selectors receive `ava.EvalContext(inputs, output, trace)`:

- `ctx.inputs` is the **validated inputs** of the first successful `await agent(...)`
  call to return, not the enclosing step's arguments: an instance of the signature's
  input model, as the runtime received it. Read fields as attributes, such as
  `ctx.inputs.document`.
- `ctx.output` is that call's **validated outputs**, the same object as
  `result.outputs`, with every named output field. Here, select
  `ctx.output.review.summary` or serialize `ctx.output.review.model_dump(mode="json")`;
  `return result.outputs.review` only controls the separate value sent to downstream
  workflow nodes.
- `ctx.trace` is that invocation's complete `ava.agent.AgentTrace`, turns included:
  `ctx.trace.header` summarizes the run, and each of `ctx.trace.turns` holds what
  the model wrote (`message`), the tool calls it made (`tool_calls`) and the
  runtime's own details (`native`, such as PredictRLM's code and output). It is a
  Pydantic model, so
  serialize the part you select, for example `ctx.trace.header.model_dump(mode="json")`.

A call whose outputs fail validation does not count as successful: its trace is
still recorded for the run, but it submits no evaluation.

Capture happens before the result returns to the step body. The first
successful agent call to return wins, even if another call started earlier.
Later calls still execute and return normally but cannot replace the capture
or trigger another evaluation. Calls that fail or are cancelled do not claim it.
If no call succeeds, there is no evaluation record. Once submitted, evaluation
continues even if subsequent step postprocessing fails or is cancelled.

Each metric has a collapsed **Input** section listing qualified source paths,
such as `input.document`, `output.review.summary`, `output`, or `trace`. **Input**
refers to Jev's input; path roots refer to the captured agent call's arguments,
validated outputs, or execution trace. Bare source names have no selected field path.
Trace source paths use the agent accent color; input and output paths stay neutral.
In run views, trace labels underline only on hover: click or press Enter/Space
to open the same node's **Trace** tab and focus it. Current definitions without
execution data keep trace paths read-only.
Python inspects selector syntax without executing it. Opaque or uninspectable
callables show `custom: qualified_name` rather than inferred fields. These paths
describe selection metadata, not the serialized state sent to TypeSafe.
Run views use captured declarations; older declarations without this metadata
do not invent input paths.

Reuse the agent signature's existing input and output types. There is no mandatory
second context schema, `input_type`, or `output_type`. Optional selector annotations
can use `ava.EvalContext[pydantic.BaseModel, pydantic.BaseModel]`; they do not change runtime validation.

Only selected text, JSON objects, or JSON arrays are sent to Jev. Nested numbers
must be finite. Convert Python-only values explicitly; arbitrary objects are
not stringified. Evaluation does not open files, extract document content, or
evaluate images/media: a path is only text, not file evidence. Select already
available text/JSON when evaluating a file-producing agent.

Every turn of `ctx.trace` carries the code the agent ran and that code's output, so
large traces can exceed [Jev's context budget](https://docs.typesafe.ai/models).
Select the source facts and observable actions needed for your question instead of
sending the whole trace or silently truncating evidence.

### Answers, composites, and batching

Composites are synchronous Python functions receiving one
`ava.ClassificationResult` containing all metric answers under their names:

| Accessor | Meaning |
| --- | --- |
| `results.answers["clarity"]` | The original typed answer |
| `results.nouls["concise"].noul` | Probability of yes, in `[0, 1]`; not a Boolean or separate confidence |
| `results.scores["clarity"].score` | Fractional, probability-weighted rubric position |
| `results.scores["clarity"].legend` | The original ordered rubric |
| `results.choices["grounding"].choice` | Selected option |
| `.probabilities`, `.confidence` on Choice/Score answers | Distribution and confidence, preserved without normalization |

An `N`-level Score ranges from `0` to `N - 1`. Normalize explicitly in a
composite with `score / (N - 1)`; the three-level clarity rubric above uses
`/ 2`. Every composite must return a finite number in `[0, 1]`. There are no
`.normalized` or `.probability` aliases, composite dependencies, or extra model
calls for composites. Raw answers remain unchanged.

Avalanche groups equal selected JSON/text state with compatible evaluator
settings. Above, clarity and concision share one request; grounding and trace
evidence remain separate. Equality is based on selected content, not lambda
identity. Do not merge different states into one large object to force batching:
that exposes extra evidence to every question. There is no question-count
heuristic; ordinary service request limits still apply.

### Configure and run

Set `TYPESAFE_API_KEY` in the operator environment, in addition to credentials
for the PredictRLM agent's model provider. Evaluation configuration follows
classifier conventions:

```text
ava.Evaluations(model=..., timeout=...)
    > @ava.workflow(classifier_defaults={...})
    > model="jev-latest", timeout=10.0
```

`timeout` is the positive, finite SDK request timeout in seconds, not a workflow
deadline. `model=None` and `timeout=None` inherit defaults. Agent runtimes
and `agent_defaults` do not configure Jev. Declarations and discovery make no
model calls and need no credentials; malformed questions fail at declaration.

A TypeSafe HTTP 401 means authentication was rejected, not that the output failed
a quality check. Verify `TYPESAFE_API_KEY`; an exported value takes precedence
over `.env`. After changing credentials, restart the operator and run again.
Do not paste keys into logs or issue reports.

An HTTP 400 with `max_tokens_exceeded` means the selected state and questions
exceeded TypeSafe's model context. Avalanche shows that machine-readable error
code without logging the request body, which can contain private source records,
agent code, and outputs. Reduce the evidence selected for that metric and rerun.

Run the repository's real-agent example from the repository root:

```bash
# Log in with `uv run codex-lm auth login NAME`, or set OPENAI_API_KEY or
# ANTHROPIC_API_KEY (see examples/README.md#model-selection), plus TYPESAFE_API_KEY,
# in the environment or project .env.
uv run ava dev examples/evaluations_workflow.py
```

Before running, the agent node shows an **Evaluations** badge with its metric
count. When zoomed out, the badge shows only its icon and count. Select the node
in **Current**, then **Evals**, to inspect named metrics, question types,
expandable criteria, composite names,
and effective Jev model and timeout. **Agent definition** contains instructions,
agent inputs/outputs, models, and resources; **Step interface** contains only the
step's declared inputs and output. These tabs describe configuration, not completed judgments.
Historical run graphs retain their captured declarations after source edits.

Select `evaluations_workflow` and click **Run** without supplying JSON or files.
The source generates a synthetic checkout incident: error-rate windows, deployment
events, support tickets, and on-call notes. A real agent prepares an evidence-linked
handoff, and a deterministic final step renders it as Markdown. No customer message
is sent and no external system is changed.

After running, inspect `prepare_incident_handoff` → **Evaluations** for grounding, actionability,
publication readiness, clarity, and trace-inspection judgments. The `handoff_quality`
composite combines Noul probability, explicitly normalized Scores, and a Choice
probability. Three questions share the same source-and-handoff state and can batch
together; clarity selects two text fields, while trace inspection selects source
records and observed per-iteration code and output once rather than duplicating
the complete terminal trace.

Inspect `render_handoff` for the finished brief. The scenario deliberately includes
an unverified duplicate-charge report and too little recovery history to declare
resolution, so a useful answer must distinguish improvement from proven recovery.
The input data is synthetic; OpenAI agent calls and TypeSafe judgments are real.
Evaluation authentication errors remain visible separately from the completed brief.

### Execution, errors, and retention

Automatic evaluations run **only in operator-managed execution**, when the first
successful agent invocation returns, before step postprocessing. The operator
owns the background worker, so evaluation completion is not awaited by downstream
steps or workflow result delivery. A submitted evaluation survives later step
failure. Evaluation errors never fail, retry, route, or otherwise gate the workflow.

Each execution has a separate record with `pending`, `completed`, or `failed`
status. A successful workflow can still have pending or failed evaluations.
Selectors, invalid selected state, Jev requests, and invalid composites report
evaluation errors, not fabricated scores. Late results do not reopen or change
the workflow's terminal status. Reruns retain separate records rather than
overwriting earlier judgments.

Records survive normal coordinator completion, but are **local, in-memory
operator state**. They do not survive operator restart; this is not durable
recovery storage. Embedded Python `.run()` accepts declarations but reports
**not evaluated** and starts no automatic evaluation worker.

Clients can query `ListEvaluations` with `runId` and optional `nodeId`, or use
`GET /api/v1/runs/{run_id}/evaluations?node_id=...` on the development REST API.
The RPC returns separate records; completed records carry `resultJson` with
`classification` (raw TypeSafe answers) and `composites`. Evaluation records
are not structural workflow status updates.

Python validates classification answers and composites before publishing results.
The browser decodes and displays those values; it does not recalculate scores or
repeat the result validation rules.

The selected agent step's Evaluations tab shows one execution result, with status
and model followed by two flat, equally styled sections: **Composites** first,
then **Metrics**. Composite results are omitted when none are declared. There are
no numbered evaluation containers or boxed metric cards. Choice results list
option percentages once, with only the winner bold and turquoise. Other choice
percentages, score-level probabilities, and confidence stay neutral. Noul values
and their bars keep the value gradient. Composite values in the tab use the same
one-decimal percentage format and gradient as the node and header summaries.

Completed composites also appear on run DAG nodes, right-aligned beside the
evaluation badge, and in the selected agent's sidebar header on every run tab.
One composite shows `label: 85.6%`; multiple composites use centered dots, such as
`85.6% · 77.0% · 90.0%`. DAG nodes show the first three scores in declaration order,
followed by `and N more` when additional scores exist. The sidebar header shows
every score and wraps onto additional lines as needed. Percentages
use a continuous red–yellow–green scale: red at 0%, yellow at 50%, and the success
color (`#22c55e`) at 100%. Labels share the same color in the node and sidebar.
These summaries use one decimal place and do not show
scores for pending or failed evaluations. Compact nodes omit composite names;
their percentages and evaluation pill counts match the duration label's size.
The compact Agent label uses the same text size, with an icon matching the
evaluation pill's icon size. Detailed node labels retain their smaller sizing.
Graph and sidebar share a single run-level evaluation poll so late results update
both surfaces.

