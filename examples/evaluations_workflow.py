"""Observe a real agent's summary with native TypeSafe evaluations.

Set OPENAI_API_KEY and TYPESAFE_API_KEY in the operator environment or project
.env, then run from the repository root:
    uv run ava dev examples/evaluations_workflow.py

Select evaluations_workflow, start a run, then open the summarize_document
step's Evaluations tab. Agent calls use OpenAI; judgments use TypeSafe.
Discovery needs no credentials and makes no model calls. Embedded Python runs
do not automatically evaluate; evaluation records live only in the operator.
"""

from __future__ import annotations

from pydantic import BaseModel

import avalanche as ava


class Summary(BaseModel):
    summary: str
    next_action: str


class SummarizeDocument(ava.Signature):
    """Summarize the supplied project update for the requested audience.

    Inspect the source before writing. Preserve dates, distinguish completed
    work from outstanding work, and never invent owners or commitments. Return
    a concise summary and the single next action supported by the update.
    """

    document: str = ava.InputField(desc="Authoritative project update.")
    audience: str = ava.InputField(desc="The readers of the summary.")
    report: Summary = ava.OutputField(desc="Grounded summary and next action.")


summary_evaluations = ava.Evaluations(
    metrics={
        "clarity": ava.Metric(
            state=lambda ctx: ctx.output.summary,
            question={
                "type": "score",
                "instructions": "How understandable is this summary?",
                "criteria": ["Confusing", "Mostly clear", "Clear throughout"],
            },
        ),
        "concise": ava.Metric(
            state=lambda ctx: ctx.output.summary,
            question={
                "type": "noul",
                "instructions": "Is the summary free of unnecessary repetition?",
            },
        ),
        "grounding": ava.Metric(
            state=lambda ctx: {
                "document": ctx.inputs["document"],
                "report": ctx.output.model_dump(mode="json"),
            },
            question={
                "type": "choice",
                "instructions": "How does the report relate to the document?",
                "criteria": {
                    "supported": "All report claims are supported by the document.",
                    "unsupported": "At least one claim is unsupported or contradicted.",
                },
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
        "readability": lambda results: (
            0.7 * (results.scores["clarity"].score / 2) + 0.3 * results.nouls["concise"].noul
        ),
    },
)


@ava.source
def load_document() -> str:
    """Supply a small, explicit source document for the agent to summarize."""
    return (
        "Project update, 25 September 2026: The migration dry run completed on "
        "24 September with no missing records. Production migration has not "
        "started. The team must obtain the customer's approval of the maintenance "
        "window before scheduling production work. No production date is agreed."
    )


@ava.agent_step(SummarizeDocument, evaluations=summary_evaluations)
async def summarize_document(
    document: str, audience: str = "project stakeholders", *, agent: ava.Agent
) -> Summary:
    """Return the report itself, not the raw prediction evaluated by accident."""
    prediction = await agent(document=document, audience=audience)
    return prediction.report


@ava.workflow(
    agent_defaults={
        "lm": "openai/gpt-5.5",
        "sub_lm": "openai/gpt-5.5",
        "max_iterations": 10,
    },
    classifier_defaults={"model": "jev-latest", "timeout": 10.0},
)
def evaluations_workflow():
    return load_document() >> summarize_document()
