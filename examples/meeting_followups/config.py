"""Local meeting fixture and demo destination routing."""

import os
from datetime import date
from pathlib import Path

from model_selection import select_models

from .schema import Department, Destination

_models = select_models()
MODEL = os.getenv("MEETING_FOLLOWUPS_MODEL") or _models.lm
SUB_MODEL = os.getenv("MEETING_FOLLOWUPS_SUB_MODEL") or _models.sub_lm

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
