import avalanche as ava
from avalanche import File

from .config import (
    FEEDBACK_WORKBOOK_PATH,
    MODEL,
    PUBLISHED_OUTPUT_DIR,
    SUB_MODEL,
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
    """Load the customer feedback workbook."""
    return File.from_path(FEEDBACK_WORKBOOK_PATH)


@ava.agent_step(
    ExtractThemes,
    runtime=ava.runtimes.PredictRLM(
        lm=MODEL,
        sub_lm=SUB_MODEL,
        skills=[ava.agent.skills.spreadsheet, evidence_coding_skill],
    ),
)
async def extract_themes(workbook: File, *, agent: ava.Agent) -> ThemeReport:
    return (await agent(workbook=workbook)).outputs.report


@ava.agent_step(
    DetectRisks,
    runtime=ava.runtimes.PredictRLM(
        lm=MODEL,
        sub_lm=SUB_MODEL,
        skills=[ava.agent.skills.spreadsheet, evidence_coding_skill],
    ),
)
async def detect_risks(workbook: File, *, agent: ava.Agent) -> RiskReport:
    return (await agent(workbook=workbook)).outputs.report


@ava.step
def compose_product_review(
    themes: ThemeReport,
    risks: RiskReport,
) -> ProductReview:
    """Combine validated theme and risk reports into a product review."""
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
    runtime=ava.runtimes.PredictRLM(
        lm=MODEL,
        sub_lm=SUB_MODEL,
        skills=[ava.agent.skills.spreadsheet],
    ),
)
async def build_review_workbook(
    source_workbook: File,
    review: ProductReview,
    *,
    agent: ava.Agent,
) -> File:
    return (await agent(source_workbook=source_workbook, review=review)).outputs.workbook


@ava.agent_step(
    WriteExecutiveBrief,
    runtime=ava.runtimes.PredictRLM(
        lm=MODEL,
        sub_lm=SUB_MODEL,
        skills=[ava.agent.skills.docx],
    ),
)
async def write_executive_brief(
    review: ProductReview,
    *,
    agent: ava.Agent,
) -> File:
    return (await agent(review=review)).outputs.brief


@ava.dest
def publish_review_pack(workbook: File, brief: File) -> PublishedReviewPack:
    """Publish the review workbook and executive brief."""
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
