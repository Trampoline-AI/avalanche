"""Real embedded execution: admission, controls, scheduling and terminal output."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

import avalanche as ava
from avalanche.local_waits import LocalPrincipal, WaitRefusalError
from avalanche.policies import DecisionTimeoutError


class Approval(ava.Outcome):
    kind: Literal["approve", "reject"]


class Signed(BaseModel):
    envelope_id: str


REVIEWER = LocalPrincipal(name="reviewer", permissions=frozenset({"run:decide", "event:send"}))
SENDER = LocalPrincipal(name="sender", permissions=frozenset({"event:send"}))


def test_buffered_event_decision_and_slot_free_sleep():
    @ava.step
    async def produce(run: ava.RunContext) -> str:
        return run.step_key

    @ava.step
    async def combine(doc: str, approval: Approval, signed: Signed) -> str:
        assert approval.kind == "approve"
        return f"{doc}/{signed.envelope_id}"

    @ava.workflow
    def flow():
        doc = produce()
        review = ava.decision(
            Approval,
            prompt="Approve",
            show=doc,
            timeout=None,
            approvers=["reviewer"],
            slug="review",
        )
        signed = ava.wait_for_event(Signed, key="signed/exact", timeout=None)
        doc >> review >> signed >> ava.sleep(timedelta(days=1))
        return combine(doc, review, signed)

    run = flow().run(
        run_id="buffered", executor=ava.LocalExecutor(max_workers=1), fast_timers=True
    )
    receipt = run.send_event(
        "signed/exact", {"envelope_id": "a"}, idempotency_key="evt", principal=SENDER
    )
    assert run.wait_until_waiting(timeout=5)
    with pytest.raises(WaitRefusalError) as denied:
        run.decide(
            "review",
            {"kind": "approve"},
            idempotency_key="denied",
            principal=SENDER,
            reason="not an approver",
        )
    assert denied.value.code == "unauthorized"
    with pytest.raises(ValidationError):
        run.decide(
            "review",
            {"kind": "other"},
            idempotency_key="invalid",
            principal=REVIEWER,
            reason="invalid",
        )
    decision = run.decide(
        "review",
        {"kind": "approve"},
        idempotency_key="approved",
        principal=REVIEWER,
        reason="checked draft",
    )
    assert run.result(timeout=5) == "buffered:produce_1/a"
    assert (
        run.send_event(
            "signed/exact", {"envelope_id": "a"}, idempotency_key="evt", principal=SENDER
        )
        == receipt
    )
    assert (
        run.decide(
            "review",
            {"kind": "approve"},
            idempotency_key="approved",
            principal=REVIEWER,
            reason="checked draft",
        )
        == decision
    )
    assert decision.principal == "reviewer"
    assert decision.reason == "checked draft"
    assert decision.reviewed == "buffered:produce_1"
    with pytest.raises(WaitRefusalError) as conflict:
        run.send_event(
            "signed/exact", {"envelope_id": "b"}, idempotency_key="evt", principal=SENDER
        )
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(WaitRefusalError) as invalid_conflict:
        run.send_event("signed/exact", {"wrong": True}, idempotency_key="evt", principal=SENDER)
    assert invalid_conflict.value.code == "idempotency_conflict"
    with pytest.raises(WaitRefusalError) as closed:
        run.send_event(
            "signed/exact", {"envelope_id": "b"}, idempotency_key="new", principal=SENDER
        )
    assert closed.value.code == "run_closed"


def test_controlled_deadlines_preloads_and_cancellation():
    @ava.workflow
    def flow():
        return ava.decision(
            Approval,
            prompt="Approve",
            timeout=timedelta(days=2),
            approvers=["reviewer"],
            slug="review",
        )

    # fast_timers must not manufacture an approval timeout.
    run = flow().run(fast_timers=True)
    assert run.wait_until_waiting(timeout=5)
    assert not run.done()
    run.advance_time(timedelta(days=3))
    with pytest.raises(DecisionTimeoutError):
        run.result(timeout=5)
    preloaded = flow().run(decisions={"review": Approval(kind="approve")}, principal=REVIEWER)
    assert preloaded.result(timeout=5) == Approval(kind="approve")
    cancelled = flow().run()
    assert cancelled.wait_until_waiting(timeout=5)
    assert cancelled.cancel()
    from concurrent.futures import CancelledError

    with pytest.raises(CancelledError):
        cancelled.result(timeout=5)
    with pytest.raises(WaitRefusalError):
        cancelled.decide(
            "review",
            {"kind": "approve"},
            idempotency_key="late",
            principal=REVIEWER,
            reason="too late",
        )

    @ava.workflow
    def timeout_flow():
        return ava.wait_for_event(Signed, key="x", timeout=10)

    event = timeout_flow().run()
    assert event.wait_until_waiting(timeout=5)
    with pytest.raises(WaitRefusalError):
        event.send_event("unknown", {}, idempotency_key="unknown", principal=SENDER)
    with pytest.raises(ValidationError):
        event.send_event("x", {"wrong": 1}, idempotency_key="invalid", principal=SENDER)
    event.advance_time(timedelta(seconds=11))
    assert event.result(timeout=5) is None


def test_shared_retry_budget_terminal_errors_and_reconciliation():
    keys = []

    @ava.source(retries=2)
    async def retry(run: ava.RunContext) -> str:
        keys.append(run.step_key)
        if len(keys) == 1:
            raise RuntimeError("synthetic transient source failure")
        if len(keys) < 3:
            raise ava.OutcomeUnknownError("synthetic uncertainty")
        return run.step_key

    @ava.workflow
    def retries():
        return retry()

    assert retries().run(run_id="retry-run").result(timeout=5) == "retry-run:retry_1"
    assert keys == ["retry-run:retry_1"] * 3
    count = []

    @ava.step(retries=5)
    async def terminal() -> None:
        count.append(1)
        raise ava.NonRetryableError("bad input")

    @ava.workflow
    def terminal_flow():
        return terminal()

    with pytest.raises(ava.NonRetryableError):
        terminal_flow().run().result(timeout=5)
    assert count == [1]

    @ava.step(retries=0)
    async def unknown() -> str:
        raise ava.OutcomeUnknownError("provider outcome unknown")

    @ava.workflow
    def no_retry():
        return unknown()

    with pytest.raises(ava.OutcomeUnknownError):
        no_retry().run().result(timeout=5)

    @ava.step(retries=0, on_uncertain="hold")
    async def held(run: ava.RunContext) -> str:
        keys.append(run.step_key)
        raise ava.OutcomeUnknownError("reconcile")

    @ava.workflow
    def hold():
        return held()

    run = hold().run(run_id="hold")
    assert run.wait_until_waiting(timeout=5)
    run.decide(
        "held_1",
        {"kind": "done", "output": "receipt"},
        idempotency_key="reconcile",
        principal=REVIEWER,
        reason="authoritative recipient lookup",
    )
    assert run.result(timeout=5) == "receipt"
    assert keys[-1] == "hold:held_1"
