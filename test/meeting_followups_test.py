"""Evidence validation and demo routing in the meeting follow-up example."""

import socket
from datetime import date
from pathlib import Path

import pytest

import avalanche as ava


@pytest.fixture(autouse=True)
def example_import_path(monkeypatch):
    # Examples are intentionally not part of the installed Avalanche package.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


def test_extraction_rejects_wrong_source_location_and_fabricated_quote():
    from examples.meeting_followups.config import DESTINATIONS
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
            destinations=DESTINATIONS,
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


def test_destination_routing_keeps_every_department_and_previews_without_credentials(
    monkeypatch,
):
    from examples.meeting_followups import flow
    from examples.meeting_followups.schema import (
        Category,
        ClassifiedFollowup,
        Department,
        Destination,
        Extraction,
        Followup,
        Passage,
    )

    @ava.step
    def extract(meeting):
        return Extraction(
            items=[
                Followup(
                    title=department.value,
                    description="Follow up after the meeting",
                    passages=[Passage(start_line=1, end_line=1, quote=meeting.lines[0])],
                )
                for department in Department
            ]
        )

    @ava.step
    def classify(extraction):
        return [
            ClassifiedFollowup(
                item_id=department.value,
                followup=item,
                category=Category.PROBLEM,
                department=department,
                answers=ava.ClassificationResult.model_validate(
                    {
                        "model": "test",
                        "answers": {
                            "department": {
                                "type": "choice",
                                "choice": department.value,
                                "probabilities": {department.value: 1.0},
                                "confidence": 1.0,
                            }
                        },
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }
                ),
            )
            for department, item in zip(Department, extraction.items, strict=True)
        ]

    def unexpected_write(*args, **kwargs):
        pytest.fail("Demo routing attempted a network connection")

    monkeypatch.setattr(flow, "extract_followups", extract)
    monkeypatch.setattr(flow, "classify_followups", classify)
    monkeypatch.setattr(socket.socket, "connect", unexpected_write)
    for variable in ("LINEAR_API_KEY", "ATTIO_API_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    def preview_departments():
        plans = flow.meeting_followups().run(executor=ava.LocalExecutor()).result(timeout=10)
        return {plan.destination: [issue.department for issue in plan.issues] for plan in plans}

    assert preview_departments() == {
        Destination.LINEAR: [Department.ENGINEERING, Department.SHARED_INTAKE],
        Destination.ATTIO: [Department.MARKETING, Department.SUPPORT],
        Destination.JIRA: [Department.PRODUCT],
    }
    # A configured remapping must not lose items or populate an unused destination.
    monkeypatch.setattr(
        flow, "DESTINATIONS", {department: Destination.ATTIO for department in Department}
    )
    assert preview_departments() == {
        Destination.LINEAR: [],
        Destination.ATTIO: list(Department),
        Destination.JIRA: [],
    }
