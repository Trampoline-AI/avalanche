"""Synthetic admission/discovery fixture; no network or credentials."""

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
    return run.step_key


@ava.transform(retries=0, on_uncertain="fail")
async def publish(value: str) -> str:
    return value


@ava.workflow
def durable_waits_flow():
    text = draft()
    review = ava.decision(
        Approval,
        prompt="Approve?",
        show=text,
        timeout=timedelta(days=2),
        on_timeout=Approval(kind="reject"),
        approvers=["reviewer"],
        slug="review",
    )
    signed = ava.wait_for_event(Signed, key="docusign.signed", timeout=None)
    pause = ava.sleep(timedelta(seconds=1))
    text >> review >> signed >> pause
    return publish(text)
