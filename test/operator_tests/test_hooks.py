"""Tests for RunHooks — callbacks fired during Workflow.run()."""


import threading
import time

import pytest

from avalanche import LocalExecutor, RayExecutor, dest, source, step, workflow
from avalanche.operator.hooks import RunHooks


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
        p.run(executor=LocalExecutor(), hooks=hooks)

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

    def test_hooks_none_is_backward_compatible(self):
        @source
        def load():
            return 42

        @workflow
        def test_pipe():
            return load()

        p = test_pipe()
        result = p.run(executor=LocalExecutor(), hooks=None)
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
            p.run(executor=LocalExecutor(), hooks=hooks)
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
        p.run(executor=LocalExecutor(), hooks=hooks)

        # Only 1 node should have started before cancellation kicked in
        # (cancel is checked at the TOP of the loop, so node 1 runs,
        # then cancel is checked before node 2)
        assert call_count <= 2  # At most the first node's function ran

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
        p.run(executor=RayExecutor(), hooks=hooks)

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

    def test_ray_hooks_do_not_serialize_independent_branches(self):
        """Independent Ray branches should submit before hook completion waits."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=4,
            ignore_reinit_error=True,
            include_dashboard=False,
            runtime_env={"working_dir": None},
        )

        try:

            @ray.remote
            class Gate:
                def __init__(self):
                    self.started = []
                    self.released = False

                def record_start(self, label):
                    self.started.append(label)

                def get_started(self):
                    return list(self.started)

                def release(self):
                    self.released = True

                def is_released(self):
                    return self.released

            gate = Gate.remote()

            @source
            def slow_a():
                import time

                import ray

                ray.get(gate.record_start.remote("a"))
                while not ray.get(gate.is_released.remote()):
                    time.sleep(0.05)
                return "a"

            @source
            def slow_b():
                import time

                import ray

                ray.get(gate.record_start.remote("b"))
                while not ray.get(gate.is_released.remote()):
                    time.sleep(0.05)
                return "b"

            @workflow
            def parallel_sources():
                return slow_a(), slow_b()

            events = []
            result = {}

            def run_workflow():
                try:
                    result["value"] = parallel_sources().run(
                        executor=RayExecutor(),
                        hooks=RunHooks(
                            on_node_success=lambda nid: events.append(("success", nid))
                        ),
                    )
                except Exception as exc:  # pragma: no cover - surfaced below
                    result["error"] = exc

            thread = threading.Thread(target=run_workflow)
            thread.start()

            try:
                deadline = time.monotonic() + 5
                started = []
                while time.monotonic() < deadline:
                    started = ray.get(gate.get_started.remote())
                    if len(started) == 2:
                        break
                    time.sleep(0.05)

                assert set(started) == {"a", "b"}
                assert events == []

                ray.get(gate.release.remote())
                thread.join(timeout=10)

                assert not thread.is_alive()
                assert result.get("error") is None
                assert result["value"] == ("a", "b")
                assert len(events) == 2
            finally:
                ray.get(gate.release.remote())
                thread.join(timeout=10)

        finally:
            ray.shutdown()

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
            runtime_env={"working_dir": None},
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

            dependent_workflow().run(
                executor=RayExecutor(),
                hooks=RunHooks(
                    on_node_success=on_success,
                    cancel_requested=lambda: canceled,
                ),
            )

            assert ray.get(recorder.get_started.remote()) == ["parent"]
        finally:
            ray.shutdown()
