"""Embedded decisions through real workers and a host's persisted step journal."""

from __future__ import annotations

from collections import Counter
from functools import wraps
from threading import current_thread
from typing import Literal

import pytest
from execution_services_test import ManagedInput, RecordingServices, _raw_input, _spec
from pydantic import BaseModel

import avalanche as ava
from runtime._async import call_sync_or_async
from runtime.operator.hooks import RunHooks


class Approval(ava.Outcome):
    kind: Literal["approve", "reject"]


class Draft(BaseModel):
    title: str


class ReviewCard(BaseModel):
    draft: Draft
    policy: str


@pytest.mark.parametrize("runtime_items", [False, True])
@pytest.mark.parametrize("with_services", [False, True])
def test_show_closure_is_a_journaled_per_item_step_and_replays_identical_cards(
    tmp_path, runtime_items, with_services
):
    calls = []
    policy = "Publication policy v1"
    drafts = [Draft(title="a"), Draft(title="b")]

    def show(draft: Draft) -> ReviewCard:
        assert current_thread().name.startswith("avalanche-local-")
        calls.append(draft.title)
        return ReviewCard(draft=draft, policy=policy)

    @ava.source
    def articles() -> list[Draft]:
        return drafts

    @ava.workflow(input=ManagedInput if with_services else None)
    def flow():
        return ava.decision.map(
            Approval,
            articles() if runtime_items else drafts,
            prompt="Publish?",
            show=show,
            timeout=None,
            slug="reviews",
        )

    # The same public hook that a host uses for step journaling records actual
    # worker outputs to disk, and supplies those outputs on the second execution.
    # No card or decision output is injected by this journal.
    journal_nodes = set()

    def journal(node_id, fn):
        if not node_id.startswith("decision_1.show["):
            return fn
        journal_nodes.add(node_id)
        path = tmp_path / f"{node_id}.json"

        @wraps(fn)
        def recorded(*args, **kwargs):
            if path.exists():
                return ReviewCard.model_validate_json(path.read_text())
            output = call_sync_or_async(fn, *args, **kwargs)
            assert isinstance(output, ReviewCard)
            path.write_text(output.model_dump_json())
            return output

        return recorded

    workflow = flow()
    declared_nodes = set(workflow.nodes)
    observed = []
    for iteration in range(2):
        services = RecordingServices()
        run = workflow.run(
            hooks=RunHooks(wrap_fn=journal),
            execution_services=_spec(services) if with_services else None,
            input=_raw_input() if with_services else None,
        )
        try:
            assert run.wait_until_waiting(timeout=5)
            # Cards can arm independently; wait until both shown outputs exist.
            with run.waits.condition:
                assert run.waits.condition.wait_for(
                    lambda: len(run.waits.snapshot()) == 2
                    and all(card["state"] == "waiting" for card in run.waits.snapshot()),
                    timeout=5,
                )
            cards = run.waits.snapshot()
            assert [card["node_id"] for card in cards] == ["decision_1[0]", "decision_1[1]"]
            shown = [card["show"] for card in cards]
            assert shown == [
                {"draft": {"title": "a"}, "policy": "Publication policy v1"},
                {"draft": {"title": "b"}, "policy": "Publication policy v1"},
            ]
            observed.append(shown)
            for index in range(2):
                receipt = run.decide(
                    f"reviews[{index}]",
                    {"kind": "approve"},
                    reason="reviewed",
                    idempotency_key=f"approve-{index}",
                )
                assert receipt.reviewed == shown[index]
            assert run.result(timeout=5) == [Approval(kind="approve")] * 2
            assert set(workflow.nodes) == declared_nodes
            if with_services:
                assert {r.node_id for r in run.execution_receipts(timeout=5)} == {
                    "decision_1.show[0]",
                    "decision_1.show[1]",
                }
                opens = [event for event in services.events if event[1] == "open"]
                for event in opens:
                    if ".show[" in event[0]:
                        assert [receipt["node"] for receipt in event[3]] == (
                            ["articles_1"] if runtime_items else []
                        )
        finally:
            run.cancel()
        # Any re-evaluation would render a different card and increment calls.
        policy = "CHANGED after journal commit"
    assert observed[0] == observed[1]
    assert Counter(calls) == {"a": 1, "b": 1}
    assert journal_nodes == {"decision_1.show[0]", "decision_1.show[1]"}
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "decision_1.show[0].json",
        "decision_1.show[1].json",
    ]


@pytest.mark.parametrize("terminal", [False, True])
def test_show_failure_uses_per_item_step_retry_and_failure_path(terminal):
    calls = Counter()
    failures = []
    successes = []

    def show(draft: Draft) -> ReviewCard:
        calls[draft.title] += 1
        if draft.title == "bad":
            if terminal:
                raise ava.NonRetryableError("invalid review input")
            raise RuntimeError("review rendering failed")
        return ReviewCard(draft=draft, policy="shared")

    @ava.workflow
    def flow():
        return ava.decision.map(
            Approval,
            [Draft(title="ok"), Draft(title="bad")],
            prompt="Publish?",
            timeout=None,
            show=show,
        )

    run = flow().run(
        executor=ava.LocalExecutor(max_workers=1),
        hooks=RunHooks(
            on_node_failure=lambda node, error: failures.append((node, type(error))),
            on_node_success=successes.append,
        ),
    )
    error = ava.NonRetryableError if terminal else RuntimeError
    with pytest.raises(error, match="invalid review input|review rendering failed"):
        run.result(timeout=5)
    assert calls == {"ok": 1, "bad": 1 if terminal else 8}
    assert ("decision_1.show[1]", error) in failures
    assert ("decision_1[1]", error) in failures
    assert "decision_1.show[0]" in successes
    assert not any(card["state"] == "waiting" for card in run.waits.snapshot())


def test_show_transient_retry_and_bound_before_rendering():
    calls = Counter()

    def show(draft: Draft) -> ReviewCard:
        calls[draft.title] += 1
        if draft.title == "b" and calls["b"] == 1:
            raise RuntimeError("transient")
        return ReviewCard(draft=draft, policy="shared")

    @ava.source
    def articles() -> list[Draft]:
        return [Draft(title="a"), Draft(title="b")]

    maximum = 8

    @ava.workflow
    def flow():
        return ava.decision.map(
            Approval,
            articles(),
            prompt="Publish?",
            timeout=None,
            show=show,
            max_items=maximum,
            slug="reviews",
        )

    run = flow().run(
        executor=ava.LocalExecutor(max_workers=1),
        decisions={
            "reviews[0]": Approval(kind="approve"),
            "reviews[1]": Approval(kind="reject"),
        },
    )
    assert run.result(timeout=5) == [Approval(kind="approve"), Approval(kind="reject")]
    assert calls == {"a": 1, "b": 2}
    calls.clear()
    maximum = 1
    overflow = flow().run()
    with pytest.raises(ValueError, match="map_overflow"):
        overflow.result(timeout=5)
    assert calls == {}
    assert overflow.waits.snapshot() == []
