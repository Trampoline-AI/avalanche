"""Real Ray tasks must not deserialize intermediate payloads on the driver."""

import asyncio
import os

import pytest

import avalanche as ava
from runtime.operator.hooks import RunHooks

pytestmark = pytest.mark.ray


@pytest.fixture(scope="module")
def ray_runtime():
    ray = pytest.importorskip("ray")
    owns_runtime = not ray.is_initialized()
    if owns_runtime:
        ray.init(num_cpus=2, include_dashboard=False)
    yield ray
    if owns_runtime:
        ray.shutdown()


@pytest.fixture
def worker_payload():
    driver_pid = os.getpid()

    def restore(value):
        if os.getpid() == driver_pid:
            raise AssertionError("intermediate payload deserialized on driver")
        return WorkerPayload(value)

    class WorkerPayload:
        def __init__(self, value):
            self.value = value

        def __reduce__(self):
            return restore, (self.value,)

    return WorkerPayload


def test_indexed_refs_reach_async_consumers_with_lineage(ray_runtime, worker_payload):
    @ava.source(num_returns=2)
    def split():
        return worker_payload("left"), worker_payload("right")

    @ava.source
    def packed():
        return worker_payload("first"), worker_payload("second")

    @ava.dest
    async def combine(left, right, ctx: ava.RunContext):
        await asyncio.sleep(0)
        return left.value + "/" + right.value, dict(ctx.lineage_vector)

    @ava.workflow
    def flow():
        pair = split()
        container = packed()
        return combine(right=container[0], left=pair[1])

    completed = []
    handle = flow().run(
        executor=ava.RayExecutor(),
        run_id="refs-run",
        hooks=RunHooks(on_node_success=completed.append),
    )
    assert handle.result(timeout=30) == (
        "right/first",
        {"split": "refs-run", "packed": "refs-run"},
    )
    assert set(completed) == {"split_1", "packed_1", "combine_1"}


def test_no_return_drains_status_without_payloads_and_surfaces_errors(
    ray_runtime, worker_payload
):
    @ava.source
    def load():
        return worker_payload("data")

    @ava.dest
    def save(value):
        return worker_payload(value.value + "-saved")

    @ava.workflow
    def background():
        load() >> save()

    completed = []
    handle = background().run(
        executor=ava.RayExecutor(),
        hooks=RunHooks(on_node_success=completed.append),
    )
    assert handle.result(timeout=30) is None
    assert completed == ["load_1", "save_1"]

    @ava.dest
    def fail(value):
        raise ValueError("cannot persist " + value.value)

    @ava.workflow
    def failing():
        load() >> fail()

    failed = failing().run(executor=ava.RayExecutor())
    with pytest.raises(ray_runtime.exceptions.RayTaskError, match="cannot persist data"):
        failed.result(timeout=30)
