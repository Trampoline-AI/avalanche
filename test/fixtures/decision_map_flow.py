"""Synthetic bounded decision-map admission fixture."""

from typing import Literal

import avalanche as ava


class Review(ava.Event):
    kind: Literal["approve", "reject"]


@ava.source(retries=2)
async def drafts() -> list[str]:
    return ["first draft", "second draft"]


@ava.workflow
def decision_map_flow():
    return ava.decision.map(
        Review,
        drafts(),
        prompt="Publish this draft?",
        timeout=10,
        on_timeout=Review(kind="reject"),
        max_items=2,
        slug="reviews",
    )
