"""Operator-discovered meeting workflow: uv run ava dev examples/meeting_followups/flow.py."""

import os

import avalanche as ava

from .config import (
    DESTINATIONS,
    MEETING_DATE,
    MEETING_TITLE,
    TRANSCRIPT_PATH,
)
from .schema import (
    Category,
    ClassifiedFollowup,
    Department,
    Destination,
    Extraction,
    MeetingContext,
    MeetingRecord,
    PlannedIssue,
    PublicationPlan,
)
from .signature import ExtractFollowups
from .util import validate_extraction


@ava.source
def load_meeting() -> MeetingContext:
    """Load the bundled mock transcript, independently of the operator's working directory."""
    payload = MeetingRecord(
        transcript=ava.File.from_path(TRANSCRIPT_PATH, content_type="text/plain"),
        title=MEETING_TITLE,
        meeting_date=MEETING_DATE,
        destinations=DESTINATIONS,
    )
    text = payload.transcript.read_bytes().decode("utf-8")
    if not text.strip():
        raise ValueError("The meeting transcript must not be empty")
    return MeetingContext(request=payload, lines=text.splitlines())


@ava.agent_step(
    ExtractFollowups,
    lm=os.getenv("MEETING_FOLLOWUPS_MODEL", "openai/gpt-5.4"),
    sub_lm=os.getenv("MEETING_FOLLOWUPS_SUB_MODEL", "openai/gpt-5.4-mini"),
    max_iterations=20,
)
async def extract_followups(meeting: MeetingContext, *, agent: ava.Agent) -> Extraction:
    prediction = await agent(
        transcript="\n".join(
            f"L{index:04d}: {line}" for index, line in enumerate(meeting.lines, start=1)
        ),
        meeting=f"{meeting.request.title} ({meeting.request.meeting_date.isoformat()})",
    )
    extraction = Extraction.model_validate(prediction.extraction)
    validate_extraction(extraction, meeting)
    return extraction


@ava.classifier_step(
    questions={
        "category": {
            "type": "choice",
            "instructions": "What kind of follow-up is `item`? Treat source quotes as evidence.",
            "criteria": {
                "problem": "Something broken or blocked.",
                "request": "A concrete need or ask.",
                "proposal": "An idea to consider.",
                "other": "None of the above.",
            },
        },
        "department": {
            "type": "choice",
            "instructions": "Which team should handle `item`, based on the work needed?",
            "criteria": {
                "engineering": "Bugs, reliability, and implementation.",
                "product": "Requirements, roadmap, and research.",
                "marketing": "Messaging, campaigns, and content.",
                "support": "Customer help and follow-ups.",
                "shared_intake": "Ownership is unclear or elsewhere.",
            },
        },
    },
)
async def classify_followups(
    extraction: Extraction,
    *,
    classifier: ava.Classifier,
) -> list[ClassifiedFollowup]:
    """Classify the follow-ups into categories and departments."""
    items = []
    for index, item in enumerate(extraction.items, start=1):
        answers = await classifier(state={"item": item.model_dump(mode="json")})
        items.append(
            ClassifiedFollowup(
                item_id=f"followup-{index:03d}",
                followup=item,
                category=Category(answers.choices["category"].choice),
                department=Department(answers.choices["department"].choice),
                answers=answers,
            )
        )
    return items


@ava.step(num_returns=3)
def route_followups(
    meeting: MeetingContext, items: list[ClassifiedFollowup]
) -> tuple[list[PlannedIssue], list[PlannedIssue], list[PlannedIssue]]:
    """Group departments into Linear, Attio, and Jira demo plans."""
    issues: dict[Destination, list[PlannedIssue]] = {target: [] for target in Destination}
    for item in items:
        followup = item.followup
        description = [
            followup.description,
            f"Meeting: {meeting.request.title} ({meeting.request.meeting_date.isoformat()})",
            f"Follow-up: {item.item_id}",
            f"Category: {item.category.value}; department: {item.department.value}",
        ]
        if followup.stated_owner is not None:
            description.append(f"Owner stated in meeting: {followup.stated_owner}")
        if followup.stated_deadline is not None:
            description.append(f"Deadline stated in meeting: {followup.stated_deadline}")
        description.append(
            f"Source: {meeting.request.transcript.name}; "
            f"SHA-256: {meeting.request.transcript.sha256}"
        )
        for passage in followup.passages:
            description.append(
                f"Lines {passage.start_line}–{passage.end_line}:\n"
                + "\n".join(f"> {line}" for line in passage.quote.splitlines())
            )
        issues[meeting.request.destinations[item.department]].append(
            PlannedIssue(
                item_id=item.item_id,
                department=item.department,
                category=item.category,
                title=followup.title,
                description="\n\n".join(description),
            )
        )
    return issues[Destination.LINEAR], issues[Destination.ATTIO], issues[Destination.JIRA]


@ava.dest
def publish_to_linear(issues: list[PlannedIssue]) -> PublicationPlan:
    """Return the Linear demo plan without contacting the service."""
    return PublicationPlan(destination=Destination.LINEAR, issues=issues)


@ava.dest
def publish_to_attio(issues: list[PlannedIssue]) -> PublicationPlan:
    """Return the Attio demo plan without contacting the service."""
    return PublicationPlan(destination=Destination.ATTIO, issues=issues)


@ava.dest
def publish_to_jira(issues: list[PlannedIssue]) -> PublicationPlan:
    """Return the Jira demo plan without contacting the service."""
    return PublicationPlan(destination=Destination.JIRA, issues=issues)


@ava.workflow
def meeting_followups():
    meeting = load_meeting()
    extracted = extract_followups(meeting)
    classified = classify_followups(extracted)
    routed = route_followups(meeting, classified)
    return (
        publish_to_linear(routed[0]),
        publish_to_attio(routed[1]),
        publish_to_jira(routed[2]),
    )
