"""Graph waits, typed decisions and stable keys, using synthetic local inputs.

Run: uv run python examples/durable_waits.py
Local waits are process-local. Restart durability belongs to a hosting runtime.
"""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

import avalanche as ava


class Approval(ava.Event):
    kind: Literal["approve", "reject"]
    note: str = ""


class Signed(BaseModel):
    envelope_id: str


@ava.step(retries=3, on_uncertain="hold")
async def draft(run: ava.RunContext) -> str:
    # A provider must enforce deduplication; this key alone is not exactly once.
    return f"contract prepared with operation key {run.step_key}"


@ava.step
async def finish(document: str, review: Approval, signature: Signed) -> str:
    if review.kind != "approve":
        raise ava.NonRetryableError("contract was rejected")
    return f"{document}; signed={signature.envelope_id}"


@ava.workflow
def contract_flow():
    document = draft()
    review = ava.decision(
        Approval,
        prompt="Approve this contract?",
        show=document,
        timeout=timedelta(days=2),
        on_timeout=Approval(kind="reject"),
        slug="review",
    )
    signature = ava.wait_for_event(Signed, key="docusign.signed", timeout=None)
    document >> review >> signature >> ava.sleep(timedelta(days=1))
    return finish(document, review, signature)


@ava.source
async def article_drafts() -> list[str]:
    return ["article-a", "article-b"]


@ava.step
async def quorum(reviews: list[Approval]) -> bool:
    # All item decisions are typed data; this step owns the publication rule.
    return sum(review.kind == "approve" for review in reviews) >= 1


class ArticleReview(BaseModel):
    article: str
    policy: str


@ava.workflow
def quorum_flow():
    publication_policy = "Publication policy v1"

    def show_review(article: str) -> ArticleReview:
        return ArticleReview(article=article, policy=publication_policy)

    reviews = ava.decision.map(
        Approval,
        article_drafts(),
        prompt="Publish this article?",
        show=show_review,
        timeout=None,
        max_items=2,
        slug="reviews",
    )
    return reviews >> quorum()


@ava.step
async def publish_all(drafts: list[str], approval: Approval) -> list[str]:
    return drafts if approval.kind == "approve" else []


@ava.workflow
def approve_all_flow():
    drafts = article_drafts()
    review = ava.decision(
        Approval, prompt="Publish this complete list?", show=drafts, timeout=None, slug="all"
    )
    return review >> publish_all(drafts, review)


if __name__ == "__main__":
    run = contract_flow().run(
        run_id="synthetic-contract",
        fast_timers=True,
        events={"docusign.signed": Signed(envelope_id="synthetic-envelope")},
        decisions={"review": Approval(kind="approve", note="checked")},
    )
    assert run.result(timeout=5).endswith("signed=synthetic-envelope")
    assert (
        quorum_flow()
        .run(
            decisions={
                "reviews[0]": Approval(kind="reject"),
                "reviews[1]": Approval(kind="approve"),
            }
        )
        .result(timeout=5)
        is True
    )
    assert approve_all_flow().run(decisions={"all": Approval(kind="approve")}).result(
        timeout=5
    ) == [
        "article-a",
        "article-b",
    ]
    print("durable_waits: local waits, per-item quorum and approve-all passed")
