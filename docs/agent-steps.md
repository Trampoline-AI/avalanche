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


@ava.workflow(agent_defaults={"lm": "openai/gpt-5.5"})
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(ExtractItems, skills=[ava.agent.skills.pdf])
        >> publish()
    )
```

Inline agent steps bind upstream values in signature input order and return
validated signature outputs: one field becomes its value, multiple fields become
an ordered tuple. Here `publish` receives `KeyItems`, not a raw prediction.

## Inline agent calls

Reuse `ReviewSignature` above with your `load_document()` source, which returns
document text:

```python
@ava.workflow(agent_defaults={"lm": "openai/gpt-5.5"})
def inline_review_flow():
    return load_document() >> ava.agent.step(ReviewSignature)
```

The workflow returns a validated `Review`. No `review_document` function is needed
for this form.

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

## Raw predictions and multiple outputs

In a decorated function, `await agent(...)` returns the raw DSPy prediction.
The function chooses what to return or save. Neither form saves outputs automatically.

`prediction.trace` contains the agent's execution trace. Its type,
`ava.agent.AgentTrace`, is the same as PredictRLM's `RunTrace`.

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


## Skills and tools

Pass `skills=` and `tools=` on the agent call or decorator, not on the signature:

```python
@ava.workflow
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(
            ExtractItems,
            skills=[ava.agent.skills.pdf],
            tools=[search_contract_repository],
        )
        >> publish()
    )
```

Tools are ordinary Python functions with unique names. Built-in skills include
`ava.agent.skills.pdf`, `.docx`, and `.spreadsheet`.

## Runtime configuration

Set shared model options with `agent_defaults`; override them on individual calls:

```python
@ava.workflow(
    agent_defaults={"lm": "openai/gpt-5.5", "max_iterations": 30},
)
def extraction_flow():
    return (
        load_pdf()
        >> ava.agent.step(
            ExtractItems,
            skills=[ava.agent.skills.pdf],
            max_iterations=60,
        )
        >> publish()
    )
```

The same options work on agent decorators. Set `verbose=True` to show the
PredictRLM trace. Set signatures, skills, and tools on each step, not in workflow
defaults.
