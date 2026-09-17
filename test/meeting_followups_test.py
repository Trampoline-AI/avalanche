"""Evidence and external-write boundaries in the meeting follow-up example."""

import io
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import avalanche as ava


@pytest.fixture(autouse=True)
def example_import_path(monkeypatch):
    # Examples are intentionally not part of the installed Avalanche package.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


def test_extraction_rejects_wrong_source_location_and_fabricated_quote():
    from examples.meeting_followups.schema import (
        Extraction,
        Followup,
        MeetingContext,
        MeetingRecord,
        Passage,
    )
    from examples.meeting_followups.util import validate_extraction

    context = MeetingContext(
        request=MeetingRecord(
            transcript=ava.File(content=b"First statement\nSecond statement"),
            title="Review",
            meeting_date=date(2026, 9, 14),
        ),
        lines=["First statement", "Second statement"],
    )
    passage = Passage(start_line=1, end_line=1, quote="Second statement")
    extraction = Extraction(
        items=[Followup(title="Follow up", description="Needs work", passages=[passage])]
    )
    with pytest.raises(ValueError):
        validate_extraction(extraction, context)
    passage.start_line = passage.end_line = 2
    validate_extraction(extraction, context)
    passage.quote = "A statement nobody made"
    with pytest.raises(ValueError):
        validate_extraction(extraction, context)


def test_publish_requires_complete_destinations_before_work_starts():
    from examples.meeting_followups.schema import MeetingRecord

    with pytest.raises(ValidationError):
        MeetingRecord(
            transcript=ava.File(content=b"A meeting"),
            title="Review",
            meeting_date=date(2026, 9, 14),
            publish=True,
        )


def test_graphql_error_stops_publication_and_preserves_confirmed_receipts(monkeypatch):
    from examples.meeting_followups import flow, linear
    from examples.meeting_followups.schema import (
        Category,
        Department,
        IssueReceipt,
        LinearDestination,
        PlannedIssue,
        PublicationPlan,
    )

    destination = LinearDestination(
        team_id=UUID(int=1),
        category_label_ids={
            category: UUID(int=index) for index, category in enumerate(Category, 2)
        },
    )
    issues = [
        PlannedIssue(
            item_id=f"item-{index}",
            department=Department.ENGINEERING,
            category=Category.PROBLEM,
            title=f"Problem {index}",
            description="Source-backed issue",
            destination=destination,
        )
        for index in range(3)
    ]
    attempts = []
    confirmed = IssueReceipt(
        item_id="item-0", identifier="ENG-1", url="https://linear.app/demo/issue/ENG-1"
    )

    def publish(issue: PlannedIssue, *, api_key: str) -> IssueReceipt:
        attempts.append(issue.item_id)
        if issue.item_id == "item-0":
            return confirmed
        return linear.create_issue(issue, api_key=api_key)

    def rejected_mutation(request, timeout):
        # GraphQL failures can arrive with HTTP 200 and partially populated data.
        return io.BytesIO(
            b'{"data":{"issueCreate":{"success":false,"issue":null}},'
            b'"errors":[{"message":"Team is inaccessible"}]}'
        )

    monkeypatch.setenv("LINEAR_API_KEY", "test-only-key")
    monkeypatch.setattr(flow, "create_issue", publish)
    monkeypatch.setattr(linear, "urlopen", rejected_mutation)
    with pytest.raises(RuntimeError) as raised:
        flow.publish_followups.fn(PublicationPlan(publish=True, issues=issues))
    assert attempts == ["item-0", "item-1"]
    assert any(confirmed.url in note for note in raised.value.__notes__)
