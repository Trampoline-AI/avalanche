from predict_rlm import File

import avalanche as ava

from .schema import ProductReview, RiskReport, ThemeReport


class ExtractThemes(ava.Signature):
    """Find recurring cross-account product needs in the feedback workbook.

    Treat the workbook as the sole source of truth. First inspect every worksheet,
    header, identifier column, row count, and join key. Analyze the complete Feedback
    sheet, using focused predict() calls for semantic coding and Python for grouping,
    deduplication, and counts.

    A theme must appear in feedback from at least two distinct accounts. Do not merge
    superficially similar requests with different underlying needs. For every theme,
    recalculate feedback, account, and segment counts from source rows and cite
    representative feedback IDs, account IDs, worksheet names, row numbers, and excerpts.
    Report the total number of feedback rows analyzed.

    Do not assess account risk, prioritize roadmap work, or create output files.
    """

    workbook: File = ava.InputField(
        desc="Excel workbook containing Feedback, Accounts, and Roadmap worksheets."
    )
    report: ThemeReport = ava.OutputField(
        desc="Recurring themes with deterministic counts and workbook evidence."
    )


class DetectRisks(ava.Signature):
    """Identify material customer risks grounded in the feedback workbook.

    Treat the workbook as the sole source of truth. Inspect and validate all worksheets,
    then join Feedback to Accounts by account_id and Roadmap by product_area. Use focused
    predict() calls to judge risk language, but use Python for joins, distinct-account
    handling, renewal comparisons, and ARR totals.

    Report a risk only when feedback describes a credible retention, compliance,
    security, or material operational impact. High severity requires explicit blocker or
    churn language plus material affected account context. Cite the exact source rows,
    list each affected account once, and make arr_at_risk_usd equal the sum of those
    distinct accounts. Report the total number of feedback rows analyzed.

    Do not discover general themes, change roadmap status, or create output files.
    """

    workbook: File = ava.InputField(
        desc="Excel workbook containing Feedback, Accounts, and Roadmap worksheets."
    )
    report: RiskReport = ava.OutputField(
        desc="Material risks with account exposure, ARR totals, and workbook evidence."
    )


class BuildReviewWorkbook(ava.Signature):
    """Render the approved product review as a validated Excel workbook.

    Use the source workbook only to reproduce cited evidence rows. Treat ProductReview as
    the complete approved analysis: do not add, remove, merge, reprioritize, or reinterpret
    findings.

    Create `/sandbox/output/workbook/product_review.xlsx` with Summary, Themes, Risks,
    and Evidence worksheets. Use formulas for summary totals where appropriate, currency
    and date formats for account exposure, readable column widths, frozen headers, filters,
    and severity-aware formatting. Every evidence row must preserve its source identifiers
    and location. Reopen the saved workbook, verify the required worksheets and formulas,
    and confirm that rendered theme and risk counts equal the supplied review.
    """

    source_workbook: File = ava.InputField(
        desc="Original workbook used only to reproduce cited evidence rows."
    )
    review: ProductReview = ava.InputField(
        desc="Approved themes and risks that the workbook must render without alteration."
    )
    workbook: File = ava.OutputField(
        desc="Validated product_review.xlsx written to the workbook output directory."
    )


class WriteExecutiveBrief(ava.Signature):
    """Render the approved product review as a concise Word executive brief.

    Treat ProductReview as the complete approved analysis. Do not introduce new evidence,
    recommendations, severity decisions, or calculations.

    Create `/sandbox/output/brief/executive_brief.docx` with an executive summary,
    recurring themes, material risks, account exposure, review questions, and a compact
    evidence appendix. Use heading styles, concise tables, readable spacing, page numbers,
    and consistent currency and date formatting. Reopen the saved document and verify the
    section order, table contents, and presence of every supplied theme and risk before
    submitting it.
    """

    review: ProductReview = ava.InputField(
        desc="Approved themes and risks to render without reinterpretation."
    )
    brief: File = ava.OutputField(
        desc="Validated executive_brief.docx written to the brief output directory."
    )
