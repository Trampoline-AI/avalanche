"""Executable local PR review loop; remote machine routing belongs to Delta."""

from datetime import timedelta

import avalanche as ava


class PR(ava.BaseInput):
    repo: str
    number: int
    head_sha: str


class ReviewReport(ava.BaseModel):
    nits: list[str]
    summary: str
    score: float


@ava.step(retries=3)
async def lint(pr: PR) -> None:
    """Replace with the repository's lint command."""


@ava.step(retries=3)
async def run_tests(pr: PR) -> None:
    """Replace with the repository's test command."""


@ava.step(retries=3, emits=("review_done", "review_done_with_nits"))
async def ai_review(pr: PR) -> ava.Event[ReviewReport]:
    report = ReviewReport(nits=[], summary=f"Reviewed {pr.head_sha}", score=0.9)
    return ava.event("review_done_with_nits" if report.nits else "review_done", report)


@ava.step
async def post_summary(report: ReviewReport) -> None:
    """Receives the review output, not the Event envelope."""
    assert isinstance(report, ReviewReport)


@ava.step(retries=3, on_uncertain="hold")
async def apply_nits(pr: PR, report: ReviewReport) -> None:
    """Apply the previous review's typed output."""
    assert isinstance(report, ReviewReport)


@ava.step(retries=3, on_uncertain="hold")
async def push_fixup(pr: PR) -> None:
    """No internal event: await the external amended event after pushing."""


@ava.step
async def notify_ready(pr: PR) -> None:
    """Notify the author that review is complete."""


@ava.step
async def cleanup(pr: PR) -> None:
    """Release resources after merge, close, or timeout."""


@ava.workflow(input=PR, output=ReviewReport, emits=("review_done", "review_done_with_nits"))
def review_round():
    review = ai_review()
    (lint() & run_tests()) >> review >> post_summary(review)
    return review


@ava.workflow(input=PR)
def amend_round():
    return apply_nits() >> push_fixup()


@ava.machine(input=PR, key=("repo", "number"), key_format="{repo}#{number}")
def pr_lifecycle():
    in_review = ava.state(review_round, initial=True)
    amend = ava.state(amend_round)
    ready_to_merge = ava.state(notify_ready)
    closed = ava.state(cleanup, final=True)

    in_review.on("review_done_with_nits") >> amend
    in_review.on("review_done") >> ready_to_merge
    amend.on("amended") >> in_review
    ready_to_merge.on("merged") >> closed
    ready_to_merge.on("closed_without_merge") >> closed
    ready_to_merge.after(timedelta(days=30)) >> closed


def test_review_loop() -> None:
    m = pr_lifecycle.test(
        PR(repo="acme/api", number=42, head_sha="a1"),
        stub={
            ai_review: [
                ava.event(
                    "review_done_with_nits",
                    ReviewReport(nits=["rename x"], summary="", score=0.7),
                ),
                ava.event("review_done", ReviewReport(nits=[], summary="", score=0.9)),
            ]
        },
    )
    assert m.state == "amend"
    m.send("amended")
    assert m.state == "ready_to_merge"
    m.send("merged")
    assert m.state == "closed"
    assert [(v.state, v.event, v.source) for v in m.history] == [
        ("in_review", "review_done_with_nits", "internal"),
        ("amend", "amended", "external"),
        ("in_review", "review_done", "internal"),
        ("ready_to_merge", "merged", "external"),
        ("closed", None, None),
    ]


if __name__ == "__main__":
    test_review_loop()
