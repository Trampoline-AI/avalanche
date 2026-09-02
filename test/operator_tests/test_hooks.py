"""Tests for RunHooks — callbacks fired during Workflow.run()."""

import queue
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from avalanche import LocalExecutor, RayExecutor, dest, source, step, workflow
from runtime.operator.hooks import RunHooks
from runtime.operator.run_worker import _QueueStream, _with_node_streams


class TestRunHooks:
    def test_hooks_fire_in_topological_order(self):
        @source
        def load():
            return "data"

        @step
        def process(data):
            return f"{data}_processed"

        @dest
        def save(data):
            return data

        @workflow
        def test_pipe():
            load() >> process() >> save()

        events = []

        hooks = RunHooks(
            on_node_start=lambda nid: events.append(("start", nid)),
            on_node_success=lambda nid: events.append(("success", nid)),
        )

        p = test_pipe()
        p.run(executor=LocalExecutor(), hooks=hooks).result()

        # Should see start/success pairs in topological order
        assert len(events) == 6  # 3 nodes * 2 events each
        starts = [nid for ev, nid in events if ev == "start"]
        successes = [nid for ev, nid in events if ev == "success"]
        assert starts == successes  # Same order
        # Events should alternate: start, success, start, success, ...
        for i in range(0, len(events), 2):
            assert events[i][0] == "start"
            assert events[i + 1][0] == "success"
            assert events[i][1] == events[i + 1][1]

    def test_local_parallel_hooks_observe_both_branches(self):
        both_running = threading.Barrier(2)
        events = []

        @source
        def left():
            both_running.wait(timeout=2)
            return "left"

        @source
        def right():
            both_running.wait(timeout=2)
            return "right"

        @dest
        def join(left_value, right_value):
            return left_value, right_value

        @workflow
        def parallel_sources():
            return (left() & right()) >> join()

        result = (
            parallel_sources()
            .run(
                executor=LocalExecutor(max_workers=2),
                hooks=RunHooks(
                    on_node_start=lambda node_id: events.append(("start", node_id)),
                    on_node_success=lambda node_id: events.append(("success", node_id)),
                ),
            )
            .result(timeout=5)
        )

        assert result == ("left", "right")
        assert {node_id for event, node_id in events[:2] if event == "start"} == {
            "left_1",
            "right_1",
        }
        assert {node_id for event, node_id in events if event == "success"} == {
            "left_1",
            "right_1",
            "join_1",
        }
        assert [event for event, _node_id in events[:2]] == ["start", "start"]

    def test_local_parallel_failure_drains_sibling_and_skips_descendant(self):
        both_running = threading.Barrier(2)
        sibling_completed = threading.Event()
        descendant_started = threading.Event()
        failures = []

        @source
        def failing():
            both_running.wait(timeout=2)
            raise ValueError("boom")

        @source
        def sibling():
            both_running.wait(timeout=2)
            sibling_completed.set()
            return "sibling"

        @step
        def descendant(_value):
            descendant_started.set()

        @workflow
        def parallel_sources():
            return (failing() >> descendant()) & sibling()

        with pytest.raises(ValueError, match="boom"):
            parallel_sources().run(
                executor=LocalExecutor(max_workers=2),
                hooks=RunHooks(
                    on_node_failure=lambda node_id, error: failures.append((node_id, error))
                ),
            ).result(timeout=5)

        assert sibling_completed.is_set()
        assert not descendant_started.is_set()
        assert [(node_id, str(error)) for node_id, error in failures] == [("failing_1", "boom")]

    def test_queue_stream_keeps_parallel_node_output_separate(self):
        event_queue = queue.Queue()
        stdout = _QueueStream(event_queue, "operator", 20)
        stderr = _QueueStream(event_queue, "operator", 40)
        both_running = threading.Barrier(2)

        def emit(label):
            stdout.write(f"{label}-")
            both_running.wait(timeout=2)
            stdout.write("done\n")

        left = _with_node_streams("left_1", lambda: emit("left"), stdout, stderr)
        right = _with_node_streams("right_1", lambda: emit("right"), stdout, stderr)

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert [future.result() for future in (pool.submit(left), pool.submit(right))] == [
                None,
                None,
            ]

        events = [event_queue.get_nowait(), event_queue.get_nowait()]
        assert {(event["node_id"], event["message"]) for event in events} == {
            ("left_1", "left-done"),
            ("right_1", "right-done"),
        }

    def test_hooks_none_is_backward_compatible(self):
        @source
        def load():
            return 42

        @workflow
        def test_pipe():
            return load()

        p = test_pipe()
        result = p.run(executor=LocalExecutor(), hooks=None).result()
        assert result == 42

    def test_failure_hook_fires_on_exception(self):
        @source
        def bad_source():
            raise ValueError("boom")

        @workflow
        def test_pipe():
            bad_source()

        events = []
        hooks = RunHooks(
            on_node_start=lambda nid: events.append(("start", nid)),
            on_node_failure=lambda nid, exc: events.append(("failure", nid, str(exc))),
        )

        p = test_pipe()
        try:
            p.run(executor=LocalExecutor(), hooks=hooks).result()
        except ValueError:
            pass

        assert any(ev[0] == "failure" for ev in events)
        failure = next(ev for ev in events if ev[0] == "failure")
        assert "boom" in failure[2]

    def test_cancel_stops_execution(self):
        call_count = 0

        @source
        def load():
            nonlocal call_count
            call_count += 1
            return "data"

        @step
        def process(data):
            nonlocal call_count
            call_count += 1
            return data

        @dest
        def save(data):
            nonlocal call_count
            call_count += 1

        @workflow
        def test_pipe():
            load() >> process() >> save()

        # Cancel immediately — should stop after first node
        cancel_after = 1
        nodes_started = []

        def on_start(nid):
            nodes_started.append(nid)

        hooks = RunHooks(
            on_node_start=on_start,
            cancel_requested=lambda: len(nodes_started) >= cancel_after,
        )

        p = test_pipe()
        with pytest.raises(CancelledError):
            p.run(executor=LocalExecutor(), hooks=hooks).result()

        # Only 1 node should have started before cancellation kicked in
        # (cancel is checked at the TOP of the loop, so node 1 runs,
        # then cancel is checked before node 2)
        assert call_count <= 2  # At most the first node's function ran

    @pytest.mark.ray
    def test_hooks_with_ray_executor(self):
        """Hooks should fire after actual completion with RayExecutor."""
        import time

        @source
        def timed_source():
            time.sleep(0.2)
            return "data"

        @step
        def timed_transform(data):
            time.sleep(0.2)
            return f"{data}_done"

        @workflow
        def test_pipe():
            timed_source() >> timed_transform()

        events = []
        timestamps = []

        def on_start(nid):
            events.append(("start", nid))
            timestamps.append(time.monotonic())

        def on_success(nid):
            events.append(("success", nid))
            timestamps.append(time.monotonic())

        hooks = RunHooks(on_node_start=on_start, on_node_success=on_success)

        p = test_pipe()
        p.run(executor=RayExecutor(), hooks=hooks).result()

        # Both nodes should have start/success pairs
        assert len(events) == 4
        starts = [(ev, nid) for ev, nid in events if ev == "start"]
        successes = [(ev, nid) for ev, nid in events if ev == "success"]
        assert len(starts) == 2
        assert len(successes) == 2

        # Success should fire AFTER the node actually ran (not immediately
        # after submit). Each node sleeps 0.2s, so there should be a gap
        # between start and success timestamps.
        start_t = timestamps[0]
        success_t = timestamps[1]
        assert success_t - start_t >= 0.1, "Success fired too early — node didn't complete"

    @pytest.mark.ray
    def test_ray_hooks_do_not_serialize_independent_branches(self):
        """Independent Ray branches should submit before hook completion waits."""

        @source
        def slow_a():
            return "a"

        @source
        def slow_b():
            return "b"

        @workflow
        def parallel_sources():
            return slow_a(), slow_b()

        events = []
        result = {}
        submitted = []
        wait_calls = []
        submitted_lock = threading.Lock()
        submitted_event = threading.Event()
        release = threading.Event()

        class FakeObjectRef:
            def __init__(self, fn, args, kwargs):
                self.fn = fn
                self.args = args
                self.kwargs = kwargs
                self.has_value = False
                self.value = None

        class FakeRay:
            ObjectRef = FakeObjectRef

            def wait(self, refs, *, num_returns=1, timeout=None):
                wait_calls.append((len(refs), num_returns, timeout))
                if timeout and timeout > 0:
                    release.wait(timeout)
                if not release.is_set():
                    return [], list(refs)
                ready = list(refs)[:num_returns]
                remaining = [ref for ref in refs if ref not in ready]
                return ready, remaining

        class DeterministicRayExecutor:
            def __init__(self):
                self.ray = FakeRay()

            def submit(self, fn, *args, num_returns=1, **kwargs):
                if num_returns != 1:
                    raise NotImplementedError("test fake only supports single-return nodes")
                with submitted_lock:
                    submitted.append(fn.__name__)
                    if {"slow_a", "slow_b"}.issubset(submitted):
                        submitted_event.set()
                return FakeObjectRef(fn, args, kwargs)

            def get(self, futures):
                return [self._resolve(future) for future in futures]

            def _resolve(self, value):
                if not isinstance(value, FakeObjectRef):
                    return value
                if not value.has_value:
                    args = [self._resolve(arg) for arg in value.args]
                    kwargs = {key: self._resolve(item) for key, item in value.kwargs.items()}
                    value.value = value.fn(*args, **kwargs)
                    value.has_value = True
                return value.value

        DeterministicRayExecutor.__name__ = "RayExecutor"
        executor = DeterministicRayExecutor()

        def run_workflow():
            try:
                result["value"] = (
                    parallel_sources()
                    .run(
                        executor=executor,
                        hooks=RunHooks(
                            on_node_success=lambda nid: events.append(("success", nid))
                        ),
                    )
                    .result()
                )
            except Exception as exc:  # pragma: no cover - surfaced below
                result["error"] = exc

        thread = threading.Thread(target=run_workflow)
        thread.start()

        try:
            assert submitted_event.wait(timeout=2), (submitted, result)
            assert set(submitted) == {"slow_a", "slow_b"}
            assert events == []

            release.set()
            thread.join(timeout=2)

            assert not thread.is_alive()
            assert result.get("error") is None
            assert result["value"] == ("a", "b")
            assert len(events) == 2
            assert wait_calls
        finally:
            release.set()
            thread.join(timeout=2)

    @pytest.mark.ray
    def test_ray_hooks_cancel_before_dependent_child_starts(self):
        """Cancellation observed after a parent succeeds should stop child submission."""
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

            @ray.remote
            class Recorder:
                def __init__(self):
                    self.started = []

                def record_start(self, label):
                    self.started.append(label)

                def get_started(self):
                    return list(self.started)

            recorder = Recorder.remote()

            @source
            def parent():
                import ray

                ray.get(recorder.record_start.remote("parent"))
                return "data"

            @step
            def child(value):
                import ray

                ray.get(recorder.record_start.remote(f"child:{value}"))
                return value

            @workflow
            def dependent_workflow():
                parent() >> child()

            canceled = False

            def on_success(_node_id):
                nonlocal canceled
                canceled = True

            with pytest.raises(CancelledError):
                dependent_workflow().run(
                    executor=RayExecutor(),
                    hooks=RunHooks(
                        on_node_success=on_success,
                        cancel_requested=lambda: canceled,
                    ),
                ).result()

            assert ray.get(recorder.get_started.remote()) == ["parent"]
        finally:
            ray.shutdown()
