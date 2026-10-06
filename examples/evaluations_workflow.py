"""Generate a synthetic checkout incident and prepare an evidence-linked handoff.

Set OPENAI_API_KEY and TYPESAFE_API_KEY in the environment or project .env, then:
    uv run ava dev examples/evaluations_workflow.py

Select evaluations_workflow and hit Run: no JSON input or files are required.
Inspect prepare_incident_handoff → Evaluations for judgments, or render_handoff
for the finished brief. Data generation is deterministic; the agent and Jev
calls are real. Nothing is sent to customers or changed in an external system.

Evaluations run only under the operator and never hold up the final report.
Missing/invalid TypeSafe credentials produce an evaluation error, not fake scores.
"""

from __future__ import annotations

from typing import Literal

from dspy import Prediction
from dspy_codex_lm import CodexLM
from pydantic import BaseModel, JsonValue

import avalanche as ava


class Evidence(BaseModel):
    evidence_id: str
    observed_at: str
    kind: Literal["metric", "deployment", "support", "on_call"]
    detail: str


class IncidentPacket(BaseModel):
    service: str
    as_of: str
    responsible_teams: list[str]
    recovery_policy: str
    records: list[Evidence]


class Finding(BaseModel):
    claim: str
    evidence_ids: list[str]


class FollowUp(BaseModel):
    action: str
    owner: str
    evidence_ids: list[str]


class IncidentHandoff(BaseModel):
    status: Literal["investigating", "monitoring", "resolved"]
    summary: str
    confirmed_findings: list[Finding]
    open_questions: list[str]
    next_actions: list[FollowUp]
    customer_update_draft: str


def handoff_evidence(
    ctx: ava.EvalContext[IncidentPacket, Prediction],
) -> dict[str, JsonValue]:
    """Shared evidence selection lets three independent questions use one request."""
    return {
        "source": ctx.inputs["packet"].model_dump(mode="json"),
        "handoff": ctx.output.handoff.model_dump(mode="json"),
    }


def handoff_trace_evidence(
    ctx: ava.EvalContext[IncidentPacket, Prediction],
) -> dict[str, JsonValue]:
    """Keep observed actions and source facts without the trace's duplicate copies."""
    actions: list[JsonValue] = []
    for event in ctx.trace:
        if event["kind"] == "trace_unavailable":
            actions.append(event)
        elif event["kind"] == "trace_finished":
            actions.append(
                {
                    "kind": "trace_finished",
                    "invocation_id": event["invocation_id"],
                    "actions": [
                        {
                            "iteration": step["iteration"],
                            "code": step["code"],
                            "output": step["output"],
                            "tool_calls": step["tool_calls"],
                            "predict_calls": step["predict_calls"],
                        }
                        for step in event["trace"]["steps"]
                    ],
                }
            )
        else:
            raise ValueError(f"Unsupported agent trace event: {event['kind']!r}")
    return {
        "source": ctx.inputs["packet"].model_dump(mode="json"),
        "agent_actions": actions,
    }


handoff_evaluations = ava.Evaluations(
    metrics={
        "grounded": ava.Metric(
            state=handoff_evidence,
            question={
                "type": "noul",
                "instructions": (
                    "Are the handoff's factual claims supported by the cited source records? "
                    "Check identifiers, chronology, error rates, and incident status. "
                    "A support allegation is not a verified duplicate charge; temporal "
                    "correlation does not confirm root cause. Proposed actions may be new, "
                    "but must not be presented as already completed."
                ),
            },
        ),
        "actionability": ava.Metric(
            state=handoff_evidence,
            question={
                "type": "score",
                "instructions": (
                    "How useful are the handoff's prioritized next actions for the next "
                    "on-call shift, given the source evidence and recovery policy?"
                ),
                "criteria": [
                    "Missing, unsafe, or unrelated actions.",
                    "Generic recommendations without clear owners or verification steps.",
                    "Relevant owned actions, but an important unresolved risk is omitted.",
                    "Specific, prioritized actions assigned to supplied teams, covering "
                    "sustained recovery verification and the unverified "
                    "duplicate-charge report.",
                ],
            },
        ),
        "publication_readiness": ava.Metric(
            state=handoff_evidence,
            question={
                "type": "choice",
                "instructions": (
                    "Assess the customer update draft against the source, not writing style "
                    "alone. Choose unsafe_claims whenever it makes an unsupported assurance; "
                    "otherwise choose needs_revision for material omissions. This judgment "
                    "does not authorize publication."
                ),
                "criteria": {
                    "ready_for_review": "Accurate, scoped draft that acknowledges uncertainty.",
                    "needs_revision": "No unsafe assurance, but important context is missing.",
                    "unsafe_claims": "Unsupported resolution, root cause, billing assurance, "
                    "or promise about when the incident will be fixed.",
                },
            },
        ),
        "clarity": ava.Metric(
            state=lambda ctx: {
                "summary": ctx.output.handoff.summary,
                "customer_update": ctx.output.handoff.customer_update_draft,
            },
            question={
                "type": "score",
                "instructions": "How understandable are these two texts to a busy reader?",
                "criteria": [
                    "Confusing, contradictory, or dominated by unexplained jargon.",
                    "Understandable, but wordy or poorly organized.",
                    "Clear, concise, and easy to scan.",
                ],
            },
        ),
        "inspected_evidence": ava.Metric(
            state=handoff_trace_evidence,
            question={
                "type": "noul",
                "instructions": (
                    "Do the recorded agent actions show inspection of source records and "
                    "a cross-check of rollback timing, error-rate recovery, and the recovery "
                    "policy before submission? Judge observable inspection, not the final "
                    "answer's claim that checking happened."
                ),
            },
        ),
    },
    composites={
        "handoff_quality": lambda results: (
            0.4 * results.nouls["grounded"].noul
            + 0.3 * (results.scores["actionability"].score / 3)
            + 0.15 * (results.scores["clarity"].score / 2)
            + 0.15 * results.choices["publication_readiness"].probabilities["ready_for_review"]
        ),
    },
)


@ava.source
def generate_mock_incident() -> IncidentPacket:
    """Generate fixed, synthetic evidence with a recovery trap and an unrelated alert."""
    records = [
        Evidence(
            evidence_id=f"METRIC-{index}",
            observed_at=f"2026-09-25T{clock}:00Z",
            kind="metric",
            detail=(
                f"In the preceding five-minute window: {failures} failed checkout requests "
                "out of 1,000 total checkout requests. These are request counts, not unique "
                "customers or confirmed payment captures."
            ),
        )
        for index, (clock, failures) in enumerate(
            [
                ("09:00", 2),
                ("09:05", 2),
                ("09:10", 3),
                ("09:15", 85),
                ("09:20", 91),
                ("09:25", 80),
                ("09:30", 40),
                ("09:35", 3),
            ],
            start=1,
        )
    ]
    records.extend(
        [
            Evidence(
                evidence_id="DEPLOY-1",
                observed_at="2026-09-25T09:12:00Z",
                kind="deployment",
                detail="Checkout version 2026.09.25.2 finished rolling out.",
            ),
            Evidence(
                evidence_id="DEPLOY-2",
                observed_at="2026-09-25T09:30:00Z",
                kind="deployment",
                detail="Payments on-call completed rollback to the previous checkout version.",
            ),
            Evidence(
                evidence_id="TICKET-1",
                observed_at="2026-09-25T09:18:00Z",
                kind="support",
                detail="Synthetic customer Cedar Demo Store reports checkout timed out twice.",
            ),
            Evidence(
                evidence_id="TICKET-2",
                observed_at="2026-09-25T09:23:00Z",
                kind="support",
                detail="Synthetic customer Birch Demo Store reports two pending bank entries "
                "after retrying checkout. Whether two payments were captured is unverified.",
            ),
            Evidence(
                evidence_id="TICKET-3",
                observed_at="2026-09-25T09:33:00Z",
                kind="support",
                detail="Cedar Demo Store reports a new checkout attempt succeeded.",
            ),
            Evidence(
                evidence_id="NOTE-1",
                observed_at="2026-09-25T09:21:00Z",
                kind="on_call",
                detail="Payments on-call suspects database connection-pool pressure. "
                "No root cause or causal link to the rollout has been confirmed.",
            ),
            Evidence(
                evidence_id="NOTE-2",
                observed_at="2026-09-25T09:31:00Z",
                kind="on_call",
                detail="Errors are falling after rollback. Payment reconciliation for "
                "TICKET-2 has not been performed. No customer update has been published.",
            ),
            Evidence(
                evidence_id="NOTE-3",
                observed_at="2026-09-25T09:07:00Z",
                kind="on_call",
                detail="A separate search-service CPU alert cleared at 09:07. "
                "It did not affect checkout and is not part of this incident.",
            ),
        ]
    )
    return IncidentPacket(
        service="Checkout — synthetic training incident",
        as_of="2026-09-25T09:35:00Z",
        responsible_teams=["Payments on-call", "Support", "Incident commander"],
        recovery_policy=(
            "After mitigation, require at least 15 consecutive minutes below a 1% "
            "checkout error rate AND completion of the duplicate-charge investigation "
            "before declaring resolution. Distinguish pending bank authorizations from "
            "confirmed duplicate captures. Do not infer affected-customer counts from "
            "request counts. Customer communication is a draft for human review."
        ),
        records=sorted(records, key=lambda record: (record.observed_at, record.evidence_id)),
    )


@ava.agent_step(
    ava.Signature(
        "packet: IncidentPacket -> handoff: IncidentHandoff",
        """Prepare an incident handoff for the next on-call shift using only this packet.

        Inspect the records, calculate the error-rate trend, and reconcile the deployment,
        support, and on-call timelines. Apply the supplied recovery policy at packet.as_of;
        improving metrics alone do not establish resolution. Exclude unrelated alerts.

        Separate confirmed observations from hypotheses and unresolved customer reports.
        Cite exact evidence IDs for every finding and next action. Prioritize concrete
        actions and assign only teams from responsible_teams. Never invent customer counts,
        a confirmed root cause, completed investigations, deadlines, or billing assurances.

        Return a concise summary, current status, confirmed findings, open questions,
        owned next actions, and a short customer-update draft. Verify citations and check
        the draft for unsupported promises before submitting. Do not send the draft,
        contact customers, browse, or modify any systems. This is synthetic training data.
        """,
        custom_types={"IncidentPacket": IncidentPacket, "IncidentHandoff": IncidentHandoff},
    ),
    evaluations=handoff_evaluations,
)
async def prepare_incident_handoff(
    packet: IncidentPacket, *, agent: ava.Agent
) -> IncidentHandoff:
    prediction = await agent(packet=packet)
    return prediction.handoff


@ava.step
def render_handoff(handoff: IncidentHandoff) -> str:
    """Produce a readable deliverable without waiting for background evaluations."""
    lines = [
        "# Checkout incident handoff — synthetic training data",
        f"**Status:** {handoff.status}",
        handoff.summary,
        "## Confirmed findings",
        *(
            f"- {item.claim} [{', '.join(item.evidence_ids)}]"
            for item in handoff.confirmed_findings
        ),
        "## Open questions",
        *(f"- {question}" for question in handoff.open_questions),
        "## Next actions",
        *(
            f"{index}. **{item.owner}:** {item.action} [{', '.join(item.evidence_ids)}]"
            for index, item in enumerate(handoff.next_actions, start=1)
        ),
        "## Customer update — draft, not sent",
        handoff.customer_update_draft,
    ]
    return "\n\n".join(lines)


@ava.workflow(
    agent_defaults={
        "lm": CodexLM(model="gpt-5.6-terra"),
        "sub_lm": CodexLM(model="gpt-5.6-terra"),
        "max_iterations": 10,
    },
    classifier_defaults={"model": "jev-latest", "timeout": 30.0},
)
def evaluations_workflow():
    return generate_mock_incident() >> prepare_incident_handoff() >> render_handoff()
