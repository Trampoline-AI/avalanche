from typing import Literal

from pydantic import BaseModel, Field

CustomerSegment = Literal["enterprise", "mid_market", "small_business", "startup"]
RiskSeverity = Literal["low", "medium", "high"]


class EvidenceReference(BaseModel):
    feedback_id: str
    account_id: str
    sheet: str
    row_number: int = Field(ge=2)
    excerpt: str


class Theme(BaseModel):
    name: str
    summary: str
    feedback_count: int = Field(ge=2)
    account_count: int = Field(ge=2)
    segment_counts: dict[CustomerSegment, int]
    evidence: list[EvidenceReference] = Field(min_length=2)


class ThemeReport(BaseModel):
    feedback_rows_analyzed: int = Field(ge=1)
    themes: list[Theme]


class AccountExposure(BaseModel):
    account_id: str
    account_name: str
    customer_segment: CustomerSegment
    arr_usd: int = Field(ge=0)
    renewal_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class Risk(BaseModel):
    issue: str
    severity: RiskSeverity
    rationale: str
    product_areas: list[str] = Field(min_length=1)
    affected_accounts: list[AccountExposure] = Field(min_length=1)
    arr_at_risk_usd: int = Field(ge=0)
    evidence: list[EvidenceReference] = Field(min_length=1)


class RiskReport(BaseModel):
    feedback_rows_analyzed: int = Field(ge=1)
    risks: list[Risk]


class ProductReview(BaseModel):
    feedback_rows_analyzed: int = Field(ge=1)
    themes: list[Theme]
    risks: list[Risk]


class PublishedReviewPack(BaseModel):
    workbook_path: str
    brief_path: str
