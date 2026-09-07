"""Async task bodies share the same injection and failure boundaries as sync tasks."""

import asyncio
import threading

import pytest

import avalanche as ava
from avalanche.runtime import get_current_run_context


@pytest.mark.asyncio
async def test_async_chain_injects_context_across_await_and_preserves_multiple_returns():
    @ava.source(num_returns=2)
    async def load():
        await asyncio.sleep(0)
        return [1, 2], [3, 4]

    @ava.step
    async def total(values, ctx: ava.RunContext, *, logger=ava.Logger()):
        await asyncio.sleep(0)
        assert get_current_run_context().run_id == ctx.run_id
        logger.info("Summing workflow data")
        return sum(values), ctx.metadata["tenant"]

    @ava.dest
    def save(left, right):
        return left, right

    @ava.workflow
    def flow():
        pair = load()
        return save(total(pair[0]), total(pair[1]))

    handle = flow().run(
        executor=ava.LocalExecutor(),
        context={"metadata": {"tenant": "acme"}},
    )
    assert await handle == ((3, "acme"), (7, "acme"))
    assert get_current_run_context() is None


@pytest.mark.asyncio
async def test_direct_async_submission_inside_an_event_loop():
    async def add(left, right):
        await asyncio.sleep(0)
        return left + right

    assert ava.LocalExecutor().submit(add, 2, 3) == 5


def test_async_branches_overlap_and_failure_prevents_descendants():
    both_running = threading.Barrier(2)
    called = []

    @ava.source
    async def load(value):
        await asyncio.sleep(0)
        both_running.wait(timeout=5)
        if value == "bad":
            raise ValueError("async failure")
        return value

    @ava.dest
    async def save(left, right):
        called.append((left, right))

    @ava.workflow
    def flow():
        (load("good") & load("bad")) >> save()

    with pytest.raises(ValueError, match="async failure"):
        flow().run(executor=ava.LocalExecutor(max_workers=2)).result(timeout=10)
    assert called == []
