"""Local meeting fixture and demo destination routing."""

from datetime import date
from pathlib import Path

from .schema import Department, Destination

TRANSCRIPT_PATH = Path(__file__).with_name("meeting.txt")
MEETING_TITLE = "Atlas cross-functional launch review"
MEETING_DATE = date(2026, 9, 14)

DESTINATIONS: dict[Department, Destination] = {
    Department.ENGINEERING: Destination.LINEAR,
    Department.PRODUCT: Destination.JIRA,
    Department.MARKETING: Destination.ATTIO,
    Department.SUPPORT: Destination.ATTIO,
    Department.SHARED_INTAKE: Destination.LINEAR,
}
