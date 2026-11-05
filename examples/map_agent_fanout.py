"""Runtime zip, pipeline, bounds and failure policies; offline model responses.

Requires Deno and its Pyodide startup assets (downloaded on a cold host).

Run: uv run python examples/map_agent_fanout.py
Inspect: uv run ava operator examples/map_agent_fanout.py
"""

from __future__ import annotations

import time
from threading import Event

import dspy
from dspy import JSONAdapter
from dspy.utils import DummyLM

import avalanche as ava
from avalanche.agent import Agent
from avalanche.agent.native.predict_rlm import PredictRLMTurn
from runtime.operator.hooks import RunHooks


class Draft(ava.Signature):
    """Return the supplied item's answer."""

    task: str = ava.InputField()
    answer: int = ava.OutputField()


@ava.source
def jobs() -> list[int]:
    return [0, 1, 2, 3]


@ava.step
def add(left: int, right: int, *, offset: int = 0) -> int:
    return left + right + offset


@ava.agent_step(Draft, runtime=ava.runtimes.PredictRLM(max_iterations=1))
async def write_article(value: int, *, agent: Agent) -> int:
    lm = DummyLM(
        [{"plan": "Submit", "code": f"SUBMIT(answer={value})"}, {"answer": value}],
        adapter=JSONAdapter(),
    )
    with dspy.context(lm=lm, adapter=JSONAdapter()):
        result = await agent(task=f"Return {value}")
    # PredictRLM can request a final extraction after its one code turn. Script
    # that model response too, but never hide a failed interpreter execution.
    assert result.trace.turns
    for turn in result.trace.turns:
        assert isinstance(turn.native, PredictRLMTurn), turn.native
        assert not turn.native.failed, turn.native.untruncated_output
    return result.outputs.answer


@ava.step
def batch(values: list[int]) -> list[int]:
    assert isinstance(values, list)
    return values


@ava.workflow
def map_agent_fanout():
    values = jobs()
    zipped = add.map(values, values, offset=1)
    drafts = write_article.map(zipped)
    return batch(drafts)


@ava.workflow
def map_mismatch():
    return add.map(add.map([1], [2]), add.map([1, 2], [2, 3]))


@ava.step
def prepare_article(value: int) -> int:
    # Keep later source items live long enough to inspect the pipelined group.
    if value > 0:
        time.sleep(8)
    return value


@ava.workflow
def map_agent_pipeline():
    return batch(write_article.map(prepare_article.map(jobs())))


def verify() -> None:
    expanded = []
    lifecycle = []

    def started(node_id: str) -> None:
        if "[" in node_id:
            assert any(node_id in event.children for event in expanded)
        lifecycle.append(node_id)

    hooks = RunHooks(
        on_nodes_expanded=lambda expansion: expanded.append(expansion),
        on_node_start=started,
    )
    flow = map_agent_fanout()
    assert len(flow.nodes) == 4  # Deployment contains groups, never slots.
    assert flow.required_protocol == ava.PROTOCOL_VERSION
    assert flow.run(executor=ava.LocalExecutor(max_workers=2), hooks=hooks).result() == [
        1,
        3,
        5,
        7,
    ]
    assert len(flow.nodes) == 4  # Run expansion must not mutate the deployment.
    assert len({child for event in expanded for child in event.children}) == 8
    assert all(
        "[" not in node_id or any(node_id in e.children for e in expanded)
        for node_id in lifecycle
    )

    @ava.workflow
    def empty():
        return batch(add.map([], []))

    assert empty().run().result() == []

    @ava.workflow
    def plain():
        return jobs()

    assert plain().required_protocol == ava.PROTOCOL_VERSION

    @ava.workflow
    def inline():
        return ava.agent.step.map(
            Draft, ["a", "b"], runtime=ava.runtimes.PredictRLM(max_iterations=1)
        )

    lm = DummyLM([{"plan": "Submit", "code": "SUBMIT(answer=5)"}] * 2, adapter=JSONAdapter())
    with dspy.context(lm=lm, adapter=JSONAdapter()):
        assert inline().run(executor=ava.LocalExecutor(max_workers=1)).result() == [5, 5]

    pipelined = Event()

    @ava.step
    def slow_tail(value: int) -> int:
        if value == 3:
            assert pipelined.wait(3), "chain waited for the whole source map"
        return value

    @ava.step
    def edit(value: int) -> int:
        if value == 0:
            pipelined.set()
        return value

    @ava.workflow
    def pipeline():
        return edit.map(slow_tail.map([0, 1, 2, 3]))

    assert pipeline().run(executor=ava.LocalExecutor(max_workers=1)).result() == [0, 1, 2, 3]

    @ava.workflow
    def implicit_gather():
        return add.map([1, 2], [3, 4]) >> batch()

    assert implicit_gather().run().result() == [4, 6]

    @ava.source
    def versioned() -> list[int]:
        return [1, 2]

    @ava.step
    def observe_lineage(value: int, context: ava.RunContext) -> int:
        assert context.lineage_vector["versioned"] == context.run_id
        return value

    @ava.workflow
    def mapped_lineage():
        return observe_lineage.map(versioned())

    assert mapped_lineage().run().result() == [1, 2]

    for left, right, maximum, expected in [
        ([1], [], None, "map_length_mismatch"),
        (list(range(9)), list(range(9)), None, "map_overflow"),
    ]:

        @ava.workflow
        def invalid():
            return add.map(left, right, max_items=maximum)

        try:
            invalid().run().result()
        except ValueError as exc:
            assert expected in str(exc) and "add" in str(exc)
        else:
            raise AssertionError(expected)

    @ava.workflow
    def raised_max():
        return add.map(list(range(9)), list(range(9)), max_items=9)

    assert raised_max().run().result() == [2 * i for i in range(9)]

    @ava.workflow
    def above_cap():
        return add.map([1], [1], max_items=65)

    try:
        above_cap()
    except ValueError as exc:
        assert "add" in str(exc) and "64" in str(exc)
    else:
        raise AssertionError("hard cap accepted")


    for fail_fast in (True, False):
        seen = []

        @ava.step(retries=0)
        def flaky(value: int) -> int:
            seen.append(value)
            if value in (0, 2):
                raise ValueError(f"bad item {value}")
            return value

        @ava.workflow
        def failed():
            return flaky.map([0, 1, 2, 3], fail_fast=fail_fast)

        try:
            failed().run(executor=ava.LocalExecutor(max_workers=1)).result()
        except ava.MapItemsFailed as exc:
            assert [item.index for item in exc.failures] == ([0] if fail_fast else [0, 2])
            assert all(item.child_id.startswith("flaky_1[") for item in exc.failures)
        else:
            raise AssertionError("failed map succeeded")
        assert seen == ([0] if fail_fast else [0, 1, 2, 3])
    print("runtime map: zip, chained agents, empty, bounds, failures PASS")


if __name__ == "__main__":
    verify()
