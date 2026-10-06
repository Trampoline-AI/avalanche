"""Temporary browser check: one agent node with exactly 2 composite scores.

Run: uv run ava dev examples/temp_composites_2.py
Uses the synthetic incident scenario, real Codex agents, and real Jev evaluations.
No workflow input or files are required; evaluations never gate the final brief.
"""

from dspy_codex_lm import CodexLM
from evaluations_workflow import (
    IncidentHandoff,
    IncidentPacket,
    generate_mock_incident,
    handoff_evaluations,
    handoff_evidence,
    handoff_trace_evidence,
    render_handoff,
)

import avalanche as ava

questions = handoff_evaluations.declaration_metadata().metrics

composite_evaluations = ava.Evaluations(
    metrics={
        "grounded": ava.Metric(
            state=handoff_evidence,
            question=questions["grounded"].model_dump(mode="json"),
        ),
        "actionability": ava.Metric(
            state=handoff_evidence,
            question=questions["actionability"].model_dump(mode="json"),
        ),
        "publication_readiness": ava.Metric(
            state=handoff_evidence,
            question=questions["publication_readiness"].model_dump(mode="json"),
        ),
        "clarity": ava.Metric(
            state=lambda ctx: {
                "summary": ctx.output.handoff.summary,
                "customer_update": ctx.output.handoff.customer_update_draft,
            },
            question=questions["clarity"].model_dump(mode="json"),
        ),
        "inspected_evidence": ava.Metric(
            state=handoff_trace_evidence,
            question=questions["inspected_evidence"].model_dump(mode="json"),
        ),
    },
    composites={
        "handoff_quality": lambda results: (
            0.4 * results.nouls["grounded"].noul
            + 0.3 * (results.scores["actionability"].score / 3)
            + 0.15 * (results.scores["clarity"].score / 2)
            + 0.15 * results.choices["publication_readiness"].probabilities["ready_for_review"]
        ),
        "evidence_quality": lambda results: (
            0.75 * results.nouls["grounded"].noul
            + 0.25 * results.nouls["inspected_evidence"].noul
        ),
    },
)


@ava.agent_step(
    ava.Signature(
        "packet: IncidentPacket -> handoff: IncidentHandoff",
        """Prepare a concise incident handoff for the next on-call shift from this packet.

        Inspect the source records, calculate error rates from request counts, and
        reconcile deployment, support, and on-call timelines. Apply the recovery
        policy at packet.as_of; improving metrics alone do not prove resolution.
        Separate confirmed findings from hypotheses and unverified customer reports.
        Cite exact evidence IDs for every finding and next action. Assign next actions
        only to responsible_teams. Do not invent root causes, billing assurances,
        completed investigations, deadlines, or promises of resolution.
        Include status, summary, findings, open questions, owned next actions, and
        an accurate customer-update draft. Verify citations before submitting.
        This is synthetic training data. Do not contact customers or modify systems.
        """,
        custom_types={"IncidentPacket": IncidentPacket, "IncidentHandoff": IncidentHandoff},
    ),
    evaluations=composite_evaluations,
)
async def prepare_incident_handoff(
    packet: IncidentPacket, *, agent: ava.Agent
) -> IncidentHandoff:
    prediction = await agent(packet=packet)
    return prediction.handoff


@ava.workflow(
    agent_defaults={
        "lm": CodexLM(model="gpt-5.6-terra"),
        "sub_lm": CodexLM(model="gpt-5.6-terra"),
        "max_iterations": 10,
    },
    classifier_defaults={"model": "jev-latest", "timeout": 30.0},
)
def temp_composites_2():
    return generate_mock_incident() >> prepare_incident_handoff() >> render_handoff()
