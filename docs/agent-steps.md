# Agent steps

`@ava.agent_step` is an ordinary Avalanche workflow step with an injected,
callable agent. The body maps workflow values into model inputs, calls the
agent, validates or composes the raw prediction, and explicitly persists its
own result.

Agent execution is implemented on top of
[PredictRLM](https://github.com/Trampoline-AI/predict-rlm). Avalanche lazily
constructs a PredictRLM predictor when the injected agent is first called, while
the surrounding function remains an ordinary Avalanche step.


The public surface has two equivalent entry points:

```python
ava.Signature is ava.agent.Signature
ava.agent_step is ava.agent.step
```

Use root aliases for typed signature classes and `ava.agent` for the agent
integration namespace, skills, files, and inline signature factory.

## Quick start

Configure the credentials required by your PredictRLM model, then declare the
model contract, the agent-backed step, and the workflow:

```python
import avalanche as ava
from pydantic import BaseModel


class Review(BaseModel):
    summary: str
    approved: bool


class ReviewSignature(ava.Signature):
    """Review a document for publication."""

    document: str = ava.InputField(desc="Document text to review.")
    review: Review = ava.OutputField(desc="Publication decision and summary.")


@ava.agent_step(ReviewSignature, lm="openai/gpt-5.5")
async def review_document(document: str, *, agent: ava.Agent) -> Review:
    prediction = await agent(document=document)
    return prediction.review


@ava.workflow
def review_flow():
    return review_document("Avalanche composes durable data and agent steps.")


result = review_flow().run(executor=ava.LocalExecutor()).result()
print(result.summary, result.approved)
```

The `agent` argument is injected by Avalanche; callers pass only ordinary
workflow values. The step body is asynchronous because the model call is
awaitable. `Workflow.run()` returns an awaitable run handle; `.result()` is the
explicit synchronous wait above.

Use `ava.input` when the value arrives at run time instead of being fixed in the
workflow declaration:

```python
class ReviewRequest(ava.BaseInput):
    document: str


@ava.workflow(input=ReviewRequest)
def review_flow():
    return review_document(ava.input.document)


result = review_flow().run(
    executor=ava.LocalExecutor(),
    input=ReviewRequest(document="Text supplied by this workflow run."),
).result()
```

## Typed signature class

`ava.Signature` intentionally mirrors
[DSPy's Signature API](https://dspy.ai/api/signatures/Signature/). It subclasses
`dspy.Signature`; `ava.InputField` and `ava.OutputField` are direct re-exports of
the DSPy field helpers; and the inline string form delegates to DSPy's signature
factory. Type annotations carry field types, while the field helpers mark input
and output direction and optionally describe each field. Native DSPy signature
classes are also accepted directly.

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
    skills=[ava.agent.skills.pdf],
)
async def audit_rfp(
    documents: list[File],
    *,
    agent: ava.Agent,
    # The table binding remains explicit at the step declaration boundary.
    dest: ava.Table = ns.audit_results,
) -> ava.AppendResult:
    prediction = await agent(documents=documents)
    return dest.append(prediction.audit)
```

The injected `agent` parameter is required and keyword-only. It is never passed
at a workflow callsite:

```python
@ava.workflow(input=PreparedInputs)
def proposal_flow():
    return audit_rfp(documents=ava.input.rfp_documents)
```

## Inline string signature

For a small local contract, build the native DSPy signature inline:

```python
quick_answer_sig = ava.agent.Signature(
    "question: str, context: str -> answer: str, citations: list[str]",
    "Answer the question from context and cite the supporting passages.",
)


@ava.agent.step(
    quick_answer_sig,
    skills=[ava.agent.skills.pdf],
    tools=[search_internal_knowledge_base],
)
async def answer_question(
    question: str,
    context: str,
    *,
    agent: ava.Agent,
) -> str:
    prediction = await agent(question=question, context=context)
    return prediction.answer
```

Use a typed class for substantial, shared, or independently tested prompt
contracts. Use the inline form for compact local contracts.

## Raw predictions and multiple outputs

`await agent(...)` always returns the raw DSPy prediction. Avalanche never
selects an output, derives a table, or appends automatically.

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
    prediction = await agent(plan=plan)
    return dest.append(
        DraftArtifacts(
            proposal=prediction.proposal,
            compliance_matrix=prediction.compliance_matrix,
        )
    )
```

Keep local extraction, file selection, validation, logging, and output
composition in the body beside the call. Create a separate plain `@ava.step`
only when work becomes a reusable durable artifact, deserves its own
retry/rerun boundary, fans out independently, or has substantial I/O.

## Skills and tools

Skills and tools are execution capabilities of a specific agent step, not
signature metadata. A reusable signature therefore remains only the model input
and output contract:

```python
quick_answer_sig = ava.agent.Signature(
    "question: str -> answer: str",
    "Answer accurately.",
)
```

Configure every capability where the signature is used:

```python
@ava.agent_step(
    quick_answer_sig,
    skills=[ava.agent.skills.pdf, ava.agent.skills.docx],
    tools=[search_contract_repository],
)
async def answer_contract_question(..., *, agent: ava.Agent):
    ...
```

Tools are ordinary callables with unique stable `__name__` values.
`ava.agent.skills.pdf`, `.docx`, and `.spreadsheet` are lazy,
identity-preserving PredictRLM re-exports. `ava.agent.Skill` constructs custom
PredictRLM skills.

## Runtime configuration

Workflow-scoped defaults configure shared PredictRLM execution policy:

Agent steps are quiet by default (`verbose=False`); set `verbose=True` on an
individual `@ava.agent_step` or in `agent_defaults` when live PredictRLM trace
output is needed.

```python
@ava.workflow(
    input=PreparedInputs,
    agent_defaults={
        "lm": "openai/gpt-5.5",
        "sub_lm": "gemini/gemini-3.5-flash",
        "max_iterations": 30,
        "verbose": False,
    },
)
def proposal_flow():
    return audit_rfp(documents=ava.input.rfp_documents)


@ava.agent_step(AuditRfpSig, max_iterations=60)
async def expensive_audit(..., *, agent: ava.Agent):
    ...
```

Resolution order:

```text
agent-step runtime kwargs > workflow agent_defaults > Avalanche agent defaults >
PredictRLM defaults
```

Workflow defaults cannot configure `signature`, `skills`, or `tools`; those are
agent-definition capabilities.
