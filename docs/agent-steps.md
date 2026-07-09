# Agent steps

`@ava.agent_step` is an ordinary Avalanche workflow step with an injected,
callable agent. The body maps workflow values into model inputs, calls the
agent, validates or composes the raw prediction, and explicitly persists its
own result.

Agent support is optional:

```bash
uv sync --extra agent
```

The public surface has two equivalent entry points:

```python
ava.Signature is ava.agent.Signature
ava.agent_step is ava.agent.step
```

Use root aliases for typed signature classes and `ava.agent` for the optional
agent integration namespace, skills, files, and inline signature factory.

## Typed signature class

A signature class is a native DSPy signature. Type annotations carry the field
types; `ava.InputField()` and `ava.OutputField()` only mark field direction and
optionally describe it.

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


@ava.agent_step(
    AuditRfpSig,
    skills=[ava.agent.skills.pdf],
)
async def audit_rfp(
    documents: list[File],
    *,
    agent: ava.Agent,
    dest: ava.Table,
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
    skills=[ava.agent.skills.pdf],
    tools=[search_internal_knowledge_base],
)


@ava.agent.step(quick_answer_sig)
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
    dest: ava.Table,
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

A signature can declare default capabilities:

```python
quick_answer_sig = ava.agent.Signature(
    "question: str -> answer: str",
    "Answer accurately.",
    skills=[ava.agent.skills.pdf],
    tools=[search_internal_knowledge_base],
)
```

`@ava.agent_step(...)` may replace either default for a particular step:

```python
@ava.agent_step(
    quick_answer_sig,
    skills=[ava.agent.skills.docx],
    tools=[search_contract_repository],
)
async def answer_contract_question(..., *, agent: ava.Agent):
    ...
```

Explicit decorator `skills=` and `tools=` replace the corresponding signature
list; they do not concatenate. Tools are ordinary callables with unique stable
`__name__` values. `ava.agent.skills.pdf`, `.docx`, and `.spreadsheet` are lazy,
identity-preserving PredictRLM re-exports. `ava.agent.Skill` constructs custom
PredictRLM skills.

## Runtime configuration

Workflow-scoped defaults configure shared PredictRLM execution policy:

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
agent-step runtime kwargs > workflow agent_defaults > PredictRLM defaults
```

Workflow defaults cannot configure `signature`, `skills`, or `tools`; those are
agent-definition capabilities.
