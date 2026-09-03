from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError, TimeoutError

import pytest

import avalanche as ava
from runtime.operator.hooks import RunHooks


def test_run_returns_active_handle_with_canonical_context_id():
    started = threading.Event()
    release = threading.Event()

    @ava.source
    def load(ctx: ava.RunContext):
        started.set()
        assert release.wait(5)
        thread = threading.current_thread()
        return ctx.run_id, thread.name, thread.daemon

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor(), run_id="caller-run")

    assert isinstance(handle, ava.RunHandle)
    assert handle.run_id == "caller-run"
    assert started.wait(5)
    assert handle.running()
    assert not handle.done()
    assert not handle.cancel_requested()
    assert not handle.cancelled()

    release.set()
    assert handle.result(timeout=5) == (
        "caller-run",
        "avalanche-local-caller-run_0",
        False,
    )
    assert handle.done()
    assert not handle.running()
    assert handle.exception() is None


def test_generated_run_id_reaches_context():
    @ava.source
    def load(ctx: ava.RunContext):
        return ctx.run_id

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())

    assert len(handle.run_id) == 26
    assert handle.result(timeout=5) == handle.run_id


def test_result_none_failure_repeated_reads_and_timeouts():
    started = threading.Event()
    release = threading.Event()

    @ava.source
    def blocked():
        started.set()
        assert release.wait(5)

    @ava.workflow
    def none_flow():
        blocked()

    handle = none_flow().run(executor=ava.LocalExecutor())
    assert started.wait(5)
    with pytest.raises(TimeoutError):
        handle.result(timeout=0)
    with pytest.raises(TimeoutError):
        handle.exception(timeout=0)
    release.set()
    assert handle.result(timeout=5) is None
    assert handle.result() is None

    error = ValueError("boom")

    @ava.source
    def fail():
        raise error

    @ava.workflow
    def failing_flow():
        return fail()

    failed = failing_flow().run(executor=ava.LocalExecutor())
    with pytest.raises(ValueError, match="boom") as first:
        failed.result(timeout=5)
    with pytest.raises(ValueError, match="boom") as second:
        failed.result()
    assert first.value is error
    assert second.value is error
    assert failed.exception() is error


def test_thread_start_failure_returns_terminal_failed_handle(monkeypatch):
    error = RuntimeError("thread-start-boom")

    def fail_start(_thread):
        raise error

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    @ava.source
    def load():
        return "unreachable"

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())

    assert handle.done()
    assert not handle.running()
    assert handle.exception() is error
    with pytest.raises(RuntimeError, match="thread-start-boom") as raised:
        handle.result()
    assert raised.value is error


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
    assert await asyncio.to_thread(started.wait, 5)

    waiter = asyncio.ensure_future(handle)
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert handle.running()
    assert not handle.cancel_requested()
    release.set()
    assert await handle == 42


def test_cooperative_cancellation_composes_with_copied_hooks():
    started = threading.Event()
    release = threading.Event()
    downstream_called = threading.Event()
    caller_cancel_checks = 0

    def caller_cancel_requested() -> bool:
        nonlocal caller_cancel_checks
        caller_cancel_checks += 1
        return False

    hooks = RunHooks(cancel_requested=caller_cancel_requested)

    @ava.source
    def load():
        started.set()
        assert release.wait(5)
        return "data"

    @ava.step
    def downstream(value):
        downstream_called.set()
        return value

    @ava.workflow
    def flow():
        return load() >> downstream()

    handle = flow().run(executor=ava.LocalExecutor(), hooks=hooks)
    assert started.wait(5)
    assert handle.cancel()
    assert not handle.cancel()
    assert handle.cancel_requested()
    assert hooks.cancel_requested is caller_cancel_requested
    release.set()

    with pytest.raises(CancelledError):
        handle.result(timeout=5)
    with pytest.raises(CancelledError):
        handle.exception()
    assert handle.cancelled()
    assert not downstream_called.is_set()
    assert caller_cancel_checks > 0


def test_completion_or_failure_before_cancellation_observation_wins():
    completed = threading.Event()
    release = threading.Event()

    @ava.source
    def load():
        completed.set()
        assert release.wait(5)
        return "done"

    @ava.workflow
    def flow():
        return load()

    handle = flow().run(executor=ava.LocalExecutor())
    assert completed.wait(5)
    assert handle.cancel()
    release.set()
    assert handle.result(timeout=5) == "done"
    assert not handle.cancelled()


def test_concurrent_handles_isolate_identity_and_cancellation():
    both_started = threading.Barrier(3)
    release = threading.Event()

    @ava.source
    def load(ctx: ava.RunContext):
        both_started.wait(timeout=5)
        assert release.wait(5)
        return ctx.run_id

    @ava.step
    def finish(run_id: str):
        return run_id

    @ava.workflow
    def flow():
        return load() >> finish()

    workflow = flow()
    first = workflow.run(executor=ava.LocalExecutor(), run_id="first")
    second = workflow.run(executor=ava.LocalExecutor(), run_id="second")
    both_started.wait(timeout=5)
    assert first.cancel()
    release.set()

    with pytest.raises(CancelledError):
        first.result(timeout=5)
    assert second.result(timeout=5) == "second"
    assert not second.cancel_requested()


def test_workflow_never_shuts_down_caller_executor():
    class TrackingExecutor(ava.LocalExecutor):
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    @ava.source
    def load():
        return "ok"

    @ava.workflow
    def flow():
        return load()

    executor = TrackingExecutor()
    assert flow().run(executor=executor).result(timeout=5) == "ok"
    assert not executor.shutdown_called


@pytest.mark.parametrize("use_default", [False, True])
def test_ray_preparation_runs_on_caller_before_driver_thread(monkeypatch, use_default):
    calls = []
    caller_thread_id = threading.get_ident()

    class FakeRay:
        initialized = False

        def is_initialized(self):
            calls.append(("is_initialized", threading.get_ident()))
            return self.initialized

        def init(self, **kwargs):
            calls.append(("init", threading.get_ident(), kwargs))
            self.initialized = True

    executor = object.__new__(ava.RayExecutor)
    executor.ray = FakeRay()
    executor._ray_init_kwargs = {"include_dashboard": False}

    @ava.source
    def load():
        return "unused"

    @ava.workflow
    def flow():
        return load()

    workflow = flow()

    def run_driver(**kwargs):
        calls.append(("driver", threading.get_ident(), kwargs["executor"]))
        return kwargs["run_id"]

    monkeypatch.setattr(workflow, "_run_driver", run_driver)
    if use_default:

        def get_default_executor():
            calls.append(("default", threading.get_ident()))
            return executor

        monkeypatch.setattr("runtime.executor.get_default_executor", get_default_executor)
        handle = workflow.run(run_id="caller-ray-run")
    else:
        handle = workflow.run(executor=executor, run_id="caller-ray-run")

    assert handle.run_id == "caller-ray-run"
    assert handle.result(timeout=5) == "caller-ray-run"

    preparation_calls = [call for call in calls if call[0] != "driver"]
    if use_default:
        assert preparation_calls[0] == ("default", caller_thread_id)
        preparation_calls = preparation_calls[1:]
    assert preparation_calls == [
        ("is_initialized", caller_thread_id),
        ("init", caller_thread_id, {"include_dashboard": False}),
    ]
    driver_call = calls[-1]
    assert driver_call[0] == "driver"
    assert driver_call[1] != caller_thread_id
    assert driver_call[2] is executor


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
