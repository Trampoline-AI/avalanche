from __future__ import annotations

from typing import Any

import pytest

import avalanche as ava
from avalanche.operator.hooks import RunHooks

EXECUTOR_FACTORIES = [
    ava.LocalExecutor,
    pytest.param(ava.RayExecutor, marks=pytest.mark.ray),
]


@pytest.mark.parametrize("executor_factory", EXECUTOR_FACTORIES)
def test_skipped_branch_is_non_value_and_fan_in_completes(executor_factory):
    if executor_factory is ava.RayExecutor:
        pytest.importorskip("ray")

    events: list[tuple[str, str, Any]] = []

    @ava.source(slug="optional")
    def optional():
        return ava.skip("No optional rows", {"partition": "2026-07-22"})

    @ava.source(slug="required")
    def required():
        return "required-value"

    @ava.dest(slug="persist")
    def persist(value: str):
        return f"persisted:{value}"

    @ava.workflow
    def optional_workflow():
        return (optional() & required()) >> persist()

    hooks = RunHooks(
        on_node_success=lambda node_id: events.append(("success", node_id, None)),
        on_node_skip=lambda node_id, outcome: events.append(("skipped", node_id, outcome)),
    )
    executor = executor_factory()
    try:
        result = optional_workflow().run(executor=executor, hooks=hooks).result()
    finally:
        ray = getattr(executor, "ray", None)
        if ray is not None and ray.is_initialized():
            ray.shutdown()

    skipped = [(node_id, outcome) for kind, node_id, outcome in events if kind == "skipped"]
    assert result == "persisted:required-value"
    assert len(skipped) == 1
    assert skipped[0][0] == "optional_1"
    assert skipped[0][1] == ava.SkipOutcome(
        reason="No optional rows", metadata={"partition": "2026-07-22"}
    )
    assert {node_id for kind, node_id, _ in events if kind == "success"} == {
        "required_1",
        "persist_1",
    }


def test_skipped_persistence_step_appends_no_row():
    appended: list[Any] = []
    skipped: list[tuple[str, ava.SkipOutcome]] = []

    @ava.source
    def optional():
        return ava.skip("Source is empty")

    @ava.dest
    def persist(value: Any = None):
        if value is None:
            return ava.skip("Nothing to persist", {"rows": 0})
        appended.append(value)
        return value

    @ava.workflow
    def persistence_workflow():
        return optional() >> persist()

    result = (
        persistence_workflow()
        .run(
            executor=ava.LocalExecutor(),
            hooks=RunHooks(
                on_node_skip=lambda node_id, outcome: skipped.append((node_id, outcome))
            ),
        )
        .result()
    )

    assert result is None
    assert appended == []
    assert skipped == [
        ("optional_1", ava.SkipOutcome("Source is empty")),
        ("persist_1", ava.SkipOutcome("Nothing to persist", {"rows": 0})),
    ]


def test_rerun_skip_contributes_no_value_or_lineage():
    observed: dict[str, Any] = {}

    @ava.source(slug="optional")
    def optional():
        return ava.skip("Not present in rerun")

    @ava.source(slug="required")
    def required():
        return "replayed-value"

    @ava.step(slug="combine")
    def combine(value: str, ctx: ava.RunContext):
        observed["value"] = value
        observed["lineage"] = dict(ctx.lineage_vector)
        return value

    @ava.workflow
    def rerunnable_workflow():
        return (optional() & required()) >> combine()

    result = (
        rerunnable_workflow()
        .run(
            executor=ava.LocalExecutor(),
            run_id="rerun_execution",
            rerun=ava.Rerun(
                run_id="original_run",
                start=["optional", "required"],
                mode="autorun",
            ),
        )
        .result()
    )

    assert result == "replayed-value"
    assert observed == {
        "value": "replayed-value",
        "lineage": {"required": "rerun_execution"},
    }


def test_skip_copies_caller_metadata():
    metadata = {"attempt": 1}

    outcome = ava.skip("No work", metadata)
    metadata["attempt"] = 2

    assert outcome == ava.SkipOutcome("No work", {"attempt": 1})


def test_skipped_node_state_round_trips_over_operator_wire_format():
    from avalanche.operator.convert import node_state_from_proto, node_state_to_proto
    from avalanche.operator.models import NodeState, NodeStatus

    state = NodeState(
        node_id="optional_1",
        name="optional",
        node_type="source",
        status=NodeStatus.SKIPPED,
        started_at=10.0,
        ended_at=11.5,
        reason="No rows",
        metadata={"partition": "west", "attempt": 2},
    )

    restored = node_state_from_proto(node_state_to_proto(state))

    assert restored == state
