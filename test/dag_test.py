"""Graph construction contracts exercised through their resulting dataflow."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import avalanche as ava


def test_nested_parallel_chains_bind_ordered_indexed_terminals():
    @ava.source
    def seed(value):
        return value

    @ava.step(num_returns=2)
    def split(value):
        return value, value + 1

    @ava.step
    def collect(*values):
        return tuple(values)

    @ava.step
    def finish(*values, label):
        return label, values

    @ava.workflow
    def flow():
        upstream = (seed(9) >> split()[1]) & (seed(20) >> split()[0])
        downstream = (collect() >> finish(label="first")) & (
            collect() >> finish(label="second")
        )
        return upstream >> downstream

    assert flow().run(executor=ava.LocalExecutor()).result(timeout=5) == (
        ("first", (10, 20)),
        ("second", (10, 20)),
    )


def test_explicit_futures_and_dependency_edges_do_not_double_bind():
    executed = []

    @ava.source
    def load(value):
        executed.append(value)
        return value

    @ava.step
    def combine(left, right, *, suffix):
        return left + right + suffix

    @ava.workflow
    def flow():
        left, right = load("left"), load("right")
        return (left & right) >> combine(right=right, left=left, suffix="!")

    assert flow().run(executor=ava.LocalExecutor()).result(timeout=5) == "leftright!"
    assert sorted(executed) == ["left", "right"]


def test_extending_parallel_alias_does_not_change_earlier_return():
    @ava.source
    def root():
        return "root"

    @ava.step
    def branch(value, *, suffix):
        return value + suffix

    @ava.source
    def extra():
        return "extra"

    @ava.dest
    def collect(*values):
        return tuple(values)

    @ava.workflow
    def flow():
        branches = branch(suffix="-a") & branch(suffix="-b")
        original = root() >> branches
        extended = (branches & extra()) >> collect()
        return original, extended

    assert flow().run(executor=ava.LocalExecutor()).result(timeout=5) == (
        ("root-a", "root-b"),
        ("root-a", "root-b", "extra"),
    )


def test_invalid_graphs_fail_during_construction():
    @ava.source
    def load():
        return "data"

    @ava.step
    def consume(value):
        return value

    with pytest.raises(RuntimeError, match="outside of workflow context"):
        load()

    @ava.workflow
    def cyclic():
        first, second = load(), consume()
        first >> second >> first

    with pytest.raises(ValueError, match="cycle"):
        cyclic()

    @ava.source(slug="shared")
    def duplicate():
        return "duplicate"

    @ava.step(slug="shared")
    def conflicting():
        return "conflict"

    @ava.workflow
    def duplicate_slugs():
        duplicate()
        conflicting()

    with pytest.raises(ValueError, match="Duplicate node slug"):
        duplicate_slugs()

    escaped = []

    @ava.workflow
    def original():
        escaped.append(load())

    original()

    @ava.workflow
    def cross_workflow():
        return escaped[0] >> consume()

    with pytest.raises(RuntimeError, match="different workflow contexts"):
        cross_workflow()


def test_concurrent_construction_keeps_graphs_isolated():
    builders_ready = threading.Barrier(2)

    @ava.source
    def load(value):
        return value

    @ava.step
    def increment(value):
        return value + 1

    @ava.workflow
    def short():
        builders_ready.wait(timeout=5)
        return load(10) >> increment()

    @ava.workflow
    def long():
        builders_ready.wait(timeout=5)
        return load(20) >> increment() >> increment()

    with ThreadPoolExecutor(max_workers=2) as builders:
        first, second = builders.submit(short), builders.submit(long)
        workflows = first.result(timeout=5), second.result(timeout=5)

    assert [flow.run(executor=ava.LocalExecutor()).result(timeout=5) for flow in workflows] == [
        11,
        22,
    ]
