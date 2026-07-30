from predict_rlm import File

import avalanche as ava

from .config import (
    BRIEF_OUTPUT_DIR,
    FEEDBACK_WORKBOOK_PATH,
    MODEL,
    PUBLISHED_OUTPUT_DIR,
    SUB_MODEL,
    WORKBOOK_OUTPUT_DIR,
)
from .schema import ProductReview, PublishedReviewPack, RiskReport, ThemeReport
from .signature import (
    BuildReviewWorkbook,
    DetectRisks,
    ExtractThemes,
    WriteExecutiveBrief,
)
from .skills import evidence_coding_skill
from .util import publish_review_pack_files


@ava.source
def load_feedback_workbook() -> File:
    return File(path=str(FEEDBACK_WORKBOOK_PATH))


@ava.agent_step(
    ExtractThemes,
    lm=MODEL,
    sub_lm=SUB_MODEL,
    skills=[ava.agent.skills.spreadsheet, evidence_coding_skill],
)
async def extract_themes(workbook: File, *, agent: ava.Agent) -> ThemeReport:
    prediction = await agent(workbook=workbook)
    return ThemeReport.model_validate(prediction.report)


@ava.agent_step(
    DetectRisks,
    lm=MODEL,
    sub_lm=SUB_MODEL,
    skills=[ava.agent.skills.spreadsheet, evidence_coding_skill],
)
async def detect_risks(workbook: File, *, agent: ava.Agent) -> RiskReport:
    prediction = await agent(workbook=workbook)
    return RiskReport.model_validate(prediction.report)


@ava.step
def compose_product_review(
    themes: ThemeReport,
    risks: RiskReport,
) -> ProductReview:
    if themes.feedback_rows_analyzed != risks.feedback_rows_analyzed:
        raise ValueError(
            "theme and risk reports analyzed different feedback row counts: "
            f"{themes.feedback_rows_analyzed} != {risks.feedback_rows_analyzed}"
        )

    for theme in themes.themes:
        if sum(theme.segment_counts.values()) != theme.feedback_count:
            raise ValueError(f"theme {theme.name!r} segment counts do not equal feedback_count")

    for risk in risks.risks:
        account_ids = [account.account_id for account in risk.affected_accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError(f"risk {risk.issue!r} repeats an affected account")
        expected_arr = sum(account.arr_usd for account in risk.affected_accounts)
        if risk.arr_at_risk_usd != expected_arr:
            raise ValueError(f"risk {risk.issue!r} ARR total does not match affected accounts")

    return ProductReview(
        feedback_rows_analyzed=themes.feedback_rows_analyzed,
        themes=themes.themes,
        risks=risks.risks,
    )


@ava.agent_step(
    BuildReviewWorkbook,
    lm=MODEL,
    sub_lm=SUB_MODEL,
    skills=[ava.agent.skills.spreadsheet],
    output_dir=WORKBOOK_OUTPUT_DIR,
)
async def build_review_workbook(
    source_workbook: File,
    review: ProductReview,
    *,
    agent: ava.Agent,
) -> File:
    prediction = await agent(source_workbook=source_workbook, review=review)
    return File.model_validate(prediction.workbook)


@ava.agent_step(
    WriteExecutiveBrief,
    lm=MODEL,
    sub_lm=SUB_MODEL,
    skills=[ava.agent.skills.docx],
    output_dir=BRIEF_OUTPUT_DIR,
)
async def write_executive_brief(
    review: ProductReview,
    *,
    agent: ava.Agent,
) -> File:
    prediction = await agent(review=review)
    return File.model_validate(prediction.brief)


@ava.dest
def publish_review_pack(workbook: File, brief: File) -> PublishedReviewPack:
    return publish_review_pack_files(
        workbook,
        brief,
        destination=PUBLISHED_OUTPUT_DIR,
    )


@ava.workflow
def feedback_review():
    return (
        (workbook := load_feedback_workbook())
        >> ((themes := extract_themes(workbook)) & (risks := detect_risks(workbook)))
        >> (review := compose_product_review(themes, risks))
        >> (build_review_workbook(workbook, review) & write_executive_brief(review))
        >> publish_review_pack()
    )
