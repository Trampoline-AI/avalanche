"""Typed handoffs for the meeting follow-up example."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

import avalanche as ava

Text = Annotated[str, Field(min_length=1)]


class Department(StrEnum):
    ENGINEERING = "engineering"
    PRODUCT = "product"
    MARKETING = "marketing"
    SUPPORT = "support"
    SHARED_INTAKE = "shared_intake"


class Category(StrEnum):
    PROBLEM = "problem"
    REQUEST = "request"
    PROPOSAL = "proposal"
    OTHER = "other"


DEPARTMENTS = {
    Department.ENGINEERING: "Software defects, reliability, integrations, and implementation.",
    Department.PRODUCT: "Product requirements, roadmap decisions, and discovery research.",
    Department.MARKETING: "Public messaging, campaigns, website copy, and marketing emails.",
    Department.SUPPORT: "Customer assistance, support guidance, and customer follow-ups.",
    Department.SHARED_INTAKE: "No listed department owns this, or ownership is unresolved.",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinearDestination(Model):
    team_id: UUID
    category_label_ids: dict[Category, UUID]

    @model_validator(mode="after")
    def complete_labels(self) -> Self:
        if set(self.category_label_ids) != set(Category):
            raise ValueError("Configure one Linear label ID for every category")
        return self


class MeetingRecord(Model):
    transcript: ava.File
    title: Text
    meeting_date: date
    departments: dict[Department, Text] = Field(default_factory=lambda: DEPARTMENTS.copy())
    destinations: dict[Department, LinearDestination] = Field(default_factory=dict)
    publish: bool = False

    @model_validator(mode="after")
    def complete_routing(self) -> Self:
        if set(self.departments) != set(Department):
            raise ValueError("Describe every department, including shared_intake")
        if self.publish and set(self.destinations) != set(Department):
            raise ValueError("Publishing requires a Linear destination for every department")
        return self


class MeetingContext(Model):
    request: MeetingRecord
    lines: list[str]


class Passage(Model):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: Text


class Followup(Model):
    title: Text
    description: Text
    passages: list[Passage] = Field(min_length=1)
    stated_owner: str | None = None
    stated_deadline: str | None = None


class Extraction(Model):
    items: list[Followup]


class ClassifiedFollowup(Model):
    item_id: str
    followup: Followup
    category: Category
    department: Department
    answers: ava.ClassificationResult


class PlannedIssue(Model):
    item_id: str
    department: Department
    category: Category
    title: str
    description: str
    # An unconfigured destination is a legitimate preview-only state.
    destination: LinearDestination | None


class PublicationPlan(Model):
    publish: bool
    issues: list[PlannedIssue]


class IssueReceipt(Model):
    item_id: str
    identifier: str
    url: str


class PublicationReport(Model):
    plan: PublicationPlan
    receipts: list[IssueReceipt]
