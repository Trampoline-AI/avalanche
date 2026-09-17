"""Local meeting fixture and optional Linear publication settings."""

from datetime import date
from pathlib import Path

from .schema import Department, LinearDestination

TRANSCRIPT_PATH = Path(__file__).with_name("meeting.txt")
MEETING_TITLE = "Atlas cross-functional launch review"
MEETING_DATE = date(2026, 9, 14)

# Keep the demo read-only unless real Linear destinations are explicitly configured.
PUBLISH = False
DESTINATIONS: dict[Department, LinearDestination] = {}
