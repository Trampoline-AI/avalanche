"""Local scheduling, binding and output integrity."""

import threading

import polars as pl
import pytest

import avalanche as ava


def test_parallel_branches_overlap_and_fan_in_in_declaration_order():
    both_running = threading.Barrier(2)

    @ava.source
    def load(value):
        both_running.wait(timeout=5)
        return value

    @ava.dest
    def join(left, right):
        return left, right

    @ava.workflow
    def flow():
        return (load("left") & load("right")) >> join()

    assert flow().run(executor=ava.LocalExecutor(max_workers=2)).result(timeout=10) == (
        "left",
        "right",
    )


def test_multiple_returns_preserve_frames_and_explicit_keyword_binding():
    @ava.source(num_returns=2)
    def extract():
        return pl.DataFrame({"value": [10, 20]}), pl.DataFrame({"value": [30, 40]})

    @ava.step
    def double(frame):
        return frame.with_columns(pl.col("value") * 2)

    @ava.dest
    def combine(left, right, *, offset):
        return left["value"].sum() + right["value"].sum() + offset

    @ava.workflow
    def flow():
        pair = extract()
        first = double(pair[0])
        return pair, first, combine(right=pair[1], left=first, offset=1)

    pair, first, total = flow().run(executor=ava.LocalExecutor()).result(timeout=5)
    assert [frame["value"].to_list() for frame in pair] == [[10, 20], [30, 40]]
    assert first["value"].to_list() == [20, 40]
    assert total == 131


def test_no_return_workflow_waits_for_side_effects_and_propagates_failure():
    saved = []

    @ava.source
    def load():
        return [1, 2, 3]

    @ava.dest
    def save(values, *, fail):
        if fail:
            raise ValueError("storage unavailable")
        saved.extend(values)

    @ava.workflow
    def successful():
        save(load(), fail=False)

    @ava.workflow
    def failing():
        save(load(), fail=True)

    assert successful().run(executor=ava.LocalExecutor()).result(timeout=5) is None
    assert saved == [1, 2, 3]
    with pytest.raises(ValueError, match="storage unavailable"):
        failing().run(executor=ava.LocalExecutor()).result(timeout=5)
    assert saved == [1, 2, 3]


@pytest.mark.parametrize("context_type", [ava.RunContext, ava.BaseContext])
def test_wrong_return_count_fails_before_downstream_can_lose_data(context_type):
    called = []

    @ava.source(num_returns=2)
    def malformed():
        return ("only one",)

    @ava.dest
    def consume(value):
        called.append(value)

    @ava.workflow(context=context_type)
    def flow():
        return malformed()[0] >> consume()

    with pytest.raises(ValueError, match="expected to return 2 values"):
        flow().run(executor=ava.LocalExecutor()).result(timeout=5)
    assert called == []
