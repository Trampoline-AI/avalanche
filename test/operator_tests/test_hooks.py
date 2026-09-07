"""Tests for RunHooks — callbacks fired during Workflow.run()."""

import queue
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from avalanche import LocalExecutor, RayExecutor, dest, source, step, workflow
from runtime.operator.hooks import RunHooks
from runtime.operator.run_worker import _QueueStream, _with_node_streams


def test_local_parallel_hooks_observe_both_branches():
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


def test_local_parallel_failure_drains_sibling_and_skips_descendant():
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


def test_queue_stream_keeps_parallel_node_output_separate():
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


@pytest.mark.ray
def test_ray_hooks_cancel_before_dependent_child_starts():
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
