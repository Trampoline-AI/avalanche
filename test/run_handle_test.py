from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError, TimeoutError

import pytest

import avalanche as ava
from runtime.operator.hooks import RunHooks


def test_active_handle_times_out_then_caches_result_or_failure():
    started = threading.Event()
    release = threading.Event()

    @ava.source
    def load(ctx: ava.RunContext):
        started.set()
        assert release.wait(5)
        return ctx.run_id

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())
    try:
        assert started.wait(5)
        assert handle.running()
        with pytest.raises(TimeoutError):
            handle.result(timeout=0)
    finally:
        release.set()
    assert handle.result(timeout=5) == handle.run_id
    assert handle.result() == handle.run_id
    assert handle.done()
    assert not handle.cancel()

    error = ValueError("boom")

    @ava.source
    def fail():
        raise error

    @ava.workflow
    def failing():
        return fail()

    failed = failing().run(executor=ava.LocalExecutor())
    with pytest.raises(ValueError, match="boom"):
        failed.result(timeout=5)
    with pytest.raises(ValueError, match="boom"):
        failed.result()
    assert failed.exception() is error


@pytest.mark.asyncio
async def test_await_is_non_blocking_and_waiter_cancellation_is_shielded():
    started = threading.Event()
    release = threading.Event()

    @ava.source
    def load():
        started.set()
        assert release.wait(5)
        return 42

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        waiter = asyncio.ensure_future(handle)
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert handle.running()
        assert not handle.cancel_requested()
    finally:
        release.set()
    assert await handle == 42


def test_cancellation_isolates_handles_and_stops_descendants_without_mutating_hooks():
    both_started = threading.Barrier(3)
    release = threading.Event()
    finished = []

    def caller_cancel_requested():
        return False

    hooks = RunHooks(cancel_requested=caller_cancel_requested)

    @ava.source
    def load(ctx: ava.RunContext):
        both_started.wait(timeout=5)
        assert release.wait(5)
        return ctx.run_id

    @ava.step
    def finish(run_id):
        finished.append(run_id)
        return run_id

    @ava.workflow
    def flow():
        return load() >> finish()

    workflow = flow()
    executor = ava.LocalExecutor()
    first = workflow.run(executor=executor, hooks=hooks, run_id="first")
    second = workflow.run(executor=executor, hooks=hooks, run_id="second")
    try:
        both_started.wait(timeout=5)
        assert first.cancel()
        assert not first.cancel()
        assert hooks.cancel_requested is caller_cancel_requested
    finally:
        release.set()
    with pytest.raises(CancelledError):
        first.result(timeout=5)
    with pytest.raises(CancelledError):
        first.exception()
    assert first.cancelled()
    assert second.result(timeout=5) == "second"
    assert not second.cancel_requested()
    assert finished == ["second"]


def test_terminal_completion_before_cancellation_observation_wins():
    started = threading.Event()
    release = threading.Event()

    @ava.source
    def load():
        started.set()
        assert release.wait(5)
        return "done"

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())
    try:
        assert started.wait(5)
        assert handle.cancel()
    finally:
        release.set()
    assert handle.result(timeout=5) == "done"
    assert not handle.cancelled()
