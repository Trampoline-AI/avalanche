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


@pytest.mark.ray
def test_ray_run_returns_handle_before_remote_completion_and_caches_outcomes():
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
    )

    try:

        @ray.remote(max_concurrency=4)
        class Gate:
            def __init__(self):
                self.started = False
                self.released = False
                self.descendant_called = False

            async def wait(self):
                self.started = True
                while not self.released:
                    await asyncio.sleep(0.01)
                return "ray-output"

            async def wait_started(self):
                while not self.started:
                    await asyncio.sleep(0.01)
                return True

            def release(self):
                self.released = True

            def record_descendant(self):
                self.descendant_called = True

            def descendant_was_called(self):
                return self.descendant_called

        gate = Gate.remote()

        @ava.source
        def load():
            import ray

            return ray.get(gate.wait.remote())

        @ava.workflow
        def flow():
            return load()

        handle = flow().run(executor=ava.RayExecutor(), run_id="ray-run")
        assert handle.run_id == "ray-run"
        assert not handle.done()

        assert ray.get(gate.wait_started.remote(), timeout=15)
        assert handle.running()
        ray.get(gate.release.remote())
        assert handle.result(timeout=10) == "ray-output"
        assert handle.result() == "ray-output"

        cancellation_gate = Gate.remote()

        @ava.source
        def blocked_upstream():
            import ray

            return ray.get(cancellation_gate.wait.remote())

        @ava.step
        def descendant(value):
            import ray

            ray.get(cancellation_gate.record_descendant.remote())
            return True

        @ava.source
        def independent():
            return "independent"

        @ava.workflow
        def cancellable_flow():
            return (blocked_upstream() >> descendant()) & independent()

        cancelled = cancellable_flow().run(executor=ava.RayExecutor())
        assert ray.get(cancellation_gate.wait_started.remote(), timeout=15)
        assert cancelled.cancel()
        ray.get(cancellation_gate.release.remote())
        with pytest.raises(CancelledError):
            cancelled.result(timeout=10)
        assert not ray.get(cancellation_gate.descendant_was_called.remote())

        @ava.source
        def fail():
            raise RuntimeError("ray-boom")

        @ava.workflow
        def failing_flow():
            return fail()

        failed = failing_flow().run(executor=ava.RayExecutor())
        with pytest.raises(Exception, match="ray-boom") as first:
            failed.result(timeout=10)
        assert failed.exception() is first.value
    finally:
        ray.shutdown()
