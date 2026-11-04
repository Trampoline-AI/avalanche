"""Graph waits, typed decisions and stable keys, using synthetic local inputs.

Run: uv run python examples/durable_waits.py
Local waits are process-local. Restart durability belongs to a hosting runtime.
"""
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

import avalanche as ava


class Approval(ava.Outcome):
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
        Approval, prompt="Approve this contract?", show=document,
        timeout=timedelta(days=2), on_timeout=Approval(kind="reject"), slug="review",
    )
    signature = ava.wait_for_event(Signed, key="docusign.signed", timeout=None)
    document >> review >> signature >> ava.sleep(timedelta(days=1))
    return finish(document, review, signature)


if __name__ == "__main__":
    run = contract_flow().run(
        run_id="synthetic-contract", fast_timers=True,
        events={"docusign.signed": Signed(envelope_id="synthetic-envelope")},
        decisions={"review": Approval(kind="approve", note="checked")},
    )
    assert run.result(timeout=5).endswith("signed=synthetic-envelope")
    print("durable_waits: local acceptance, decision and fast sleep passed")
