"""Tests for Operator — workflow execution and state management."""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from avalanche import LocalExecutor, RayExecutor
from avalanche._agent_evidence import emit_agent_evidence
from avalanche.operator import Operator
from avalanche.operator.models import NodeState, NodeStatus, RunState, RunStatus
from avalanche.operator.operator import RunAlreadyExistsError
from avalanche.operator.scheduler import Scheduler
from runtime.operator.run_worker import (
    _QueueStream,
    _with_local_node_observers,
    _with_ray_node_observers,
)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


def arbitrary_executor_factory():
    return LocalExecutor()


class TestOperatorLifecycle:
    def _make_operator(self):
        return Operator(
            workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
            schedule=False,
            watch=False,
        )

    def test_list_workflows(self):
        op = self._make_operator()
        names = [p.name for p in op.list_workflows()]
        assert "simple_workflow" in names
        assert "slow_workflow" in names

    def test_start_run_returns_run_id(self):
        op = self._make_operator()
        run_id = op.start_run("simple_workflow")
        assert run_id.startswith("run_")

    def test_custom_run_id_reservation_rejects_sequential_duplicate(self):
        op = self._make_operator()

        assert op.start_run("simple_workflow", run_id="run_reserved") == "run_reserved"
        with pytest.raises(RunAlreadyExistsError, match="already exists"):
            op.start_run("simple_workflow", run_id="run_reserved")

    def test_custom_run_id_reservation_is_atomic_for_concurrent_requests(self):
        op = self._make_operator()

        def reserve():
            try:
                return op.start_run("simple_workflow", run_id="run_concurrent")
            except RunAlreadyExistsError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))

        assert results.count("run_concurrent") == 1
        failures = [result for result in results if isinstance(result, RunAlreadyExistsError)]
        assert len(failures) == 1

    def test_run_completes_successfully(self):
        op = self._make_operator()
        run_id = op.start_run("simple_workflow")

        # Wait for completion
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.05)

        run = op.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        assert run.ended_at is not None

    def test_start_run_passes_input_context_and_file_values_to_workflow(self):
        op = self._make_operator()
        run_id = op.start_run(
            "input_workflow",
            input={
                "message": "hello",
                "document": {"name": "note.txt", "content": b"contents"},
            },
            context={"request_id": "req_456", "run_id": "spoofed_user_id"},
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.05)

        run = op.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        messages = [entry.message for entry in run.logs]
        assert any("message=hello" in message for message in messages)
        assert any("request_id=req_456" in message for message in messages)
        assert any(f"run_id={run_id}" in message for message in messages)
        assert not any("run_id=spoofed_user_id" in message for message in messages)
        assert any("file=contents" in message for message in messages)

    def test_all_nodes_succeed(self):
        op = self._make_operator()
        run_id = op.start_run("simple_workflow")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status == RunStatus.SUCCESS:
                break
            time.sleep(0.05)

        run = op.get_run(run_id)
        for ns in run.nodes.values():
            assert ns.status == NodeStatus.SUCCESS, f"{ns.name} is {ns.status}"
            assert ns.started_at is not None
            assert ns.ended_at is not None

    def test_node_transitions_observed(self):
        """Verify nodes go through PENDING -> RUNNING -> SUCCESS in order."""
        op = self._make_operator()
        events = []

        def on_update(run):
            for ns in run.nodes.values():
                events.append((ns.node_id, ns.status))

        op.on_run_update(on_update)
        run_id = op.start_run("simple_workflow")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status == RunStatus.SUCCESS:
                break
            time.sleep(0.05)

        # Each node should have been seen as RUNNING at some point
        running_nodes = {nid for nid, status in events if status == NodeStatus.RUNNING}
        success_nodes = {nid for nid, status in events if status == NodeStatus.SUCCESS}

        run = op.get_run(run_id)
        all_node_ids = set(run.nodes.keys())
        assert running_nodes == all_node_ids, f"Missing RUNNING: {all_node_ids - running_nodes}"
        assert success_nodes == all_node_ids, f"Missing SUCCESS: {all_node_ids - success_nodes}"

    def test_list_runs_filters_by_workflow(self):
        op = self._make_operator()
        op.start_run("simple_workflow")
        time.sleep(0.1)  # let thread start

        runs = op.list_runs("simple_workflow")
        assert len(runs) == 1
        assert runs[0].flow_name == "simple_workflow"

        runs = op.list_runs("nonexistent")
        assert len(runs) == 0

    def test_refresh_invalid_file_removes_descriptor_and_schedule(self, tmp_path):
        workflow_file = tmp_path / "scheduled.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow(cron='* * * * *')\n"
            "def scheduled():\n"
            "    return None\n"
        )
        operator = Operator(workflow_paths=[str(workflow_file)], schedule=False, watch=False)
        assert [item.workflow_id for item in operator.list_workflows()] == [
            "scheduled.py::scheduled"
        ]
        assert len(operator._scheduler.list_schedules()) == 1

        workflow_file.write_text("invalid Python !!!\n")
        operator._refresh_workflows()

        assert operator.list_workflows() == []
        assert operator._scheduler.list_schedules() == []

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: LocalExecutor(),
            type("CustomLocal", (LocalExecutor,), {}),
            arbitrary_executor_factory,
        ],
    )
    def test_unsupported_executor_factories_are_rejected(self, factory):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            with pytest.raises(TypeError, match="Per-run spawn requires serializable"):
                Operator([], executor_factory=factory, watch=False, schedule=False)

    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (LocalExecutor, {"backend": "local"}),
            (
                RayExecutor,
                {"backend": "ray", "runtime_env": {}, "ray_init_kwargs": {}},
            ),
        ],
    )
    def test_deprecated_exact_executor_factories_remain_supported(self, factory, expected):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            operator = Operator([], executor_factory=factory, watch=False, schedule=False)
        try:
            assert operator._executor_config == expected
        finally:
            operator.close()

    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (LocalExecutor, {"backend": "local"}),
            (
                RayExecutor,
                {"backend": "ray", "runtime_env": {}, "ray_init_kwargs": {}},
            ),
        ],
    )
    def test_deprecated_executor_factories_remain_positional(self, factory, expected):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            operator = Operator([], factory, False, False)
        try:
            assert operator._executor_config == expected
        finally:
            operator.close()

    @pytest.mark.parametrize(
        ("watch", "schedule", "expected_calls"),
        [
            (True, False, ["watch"]),
            (False, True, ["schedule"]),
        ],
    )
    def test_positional_watch_and_schedule_keep_their_original_slots(
        self, monkeypatch, watch, schedule, expected_calls
    ):
        calls = []
        monkeypatch.setattr(Operator, "_start_watcher", lambda self: calls.append("watch"))
        monkeypatch.setattr(Scheduler, "start", lambda self: calls.append("schedule"))

        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            operator = Operator(
                [os.path.join(FIXTURES_DIR, "sample_workflows.py")],
                LocalExecutor,
                watch,
                schedule,
            )
        try:
            assert calls == expected_calls
        finally:
            operator.close()

    def test_ray_partial_preserves_spawn_safe_init_configuration(self):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            operator = Operator(
                [],
                executor_factory=partial(
                    RayExecutor,
                    ray_init_kwargs={
                        "address": "ray://cluster:10001",
                        "namespace": "dev",
                    },
                    runtime_env={"env_vars": {"MODE": "test"}},
                ),
                watch=False,
                schedule=False,
            )
        try:
            assert operator._executor_config == {
                "backend": "ray",
                "ray_init_kwargs": {
                    "address": "ray://cluster:10001",
                    "namespace": "dev",
                },
                "runtime_env": {"env_vars": {"MODE": "test"}},
            }
        finally:
            operator.close()

    def test_explicit_ray_backend_preserves_spawn_safe_init_configuration(self):
        operator = Operator(
            [],
            executor_backend="ray",
            ray_init_kwargs={
                "address": "ray://cluster:10001",
                "namespace": "dev",
            },
            ray_runtime_env={"env_vars": {"MODE": "test"}},
            watch=False,
            schedule=False,
        )
        try:
            assert operator._executor_config == {
                "backend": "ray",
                "ray_init_kwargs": {
                    "address": "ray://cluster:10001",
                    "namespace": "dev",
                },
                "runtime_env": {"env_vars": {"MODE": "test"}},
            }
        finally:
            operator.close()

    @pytest.mark.parametrize("backend", ["thread", "process"])
    def test_invalid_executor_backend_is_rejected(self, backend):
        with pytest.raises(ValueError, match="Unsupported executor_backend"):
            Operator([], executor_backend=backend, watch=False, schedule=False)

    @pytest.mark.parametrize(
        "ray_config",
        [
            {"ray_runtime_env": {}},
            {"ray_init_kwargs": {}},
        ],
    )
    def test_local_backend_rejects_ray_configuration(self, ray_config):
        with pytest.raises(ValueError, match="require executor_backend='ray'"):
            Operator([], watch=False, schedule=False, **ray_config)

    @pytest.mark.parametrize(
        "explicit_config",
        [
            {"executor_backend": "local"},
            {"executor_backend": "ray"},
            {"ray_runtime_env": {}},
            {"ray_init_kwargs": {}},
        ],
    )
    def test_deprecated_factory_conflicts_with_explicit_configuration(self, explicit_config):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            with pytest.raises(TypeError, match="cannot be combined"):
                Operator(
                    [],
                    executor_factory=LocalExecutor,
                    watch=False,
                    schedule=False,
                    **explicit_config,
                )


class TestOperatorCancellation:
    def _make_operator(self):
        return Operator(
            workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
            schedule=False,
            watch=False,
            cancel_grace=0.2,
        )

    def test_cancel_run(self):
        op = self._make_operator()
        run_id = op.start_run("slow_workflow")

        # Wait for run to start
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status == RunStatus.RUNNING:
                break
            time.sleep(0.05)

        op.cancel_run(run_id)

        # Wait for cancellation to take effect
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = op.get_run(run_id)
            if run and run.status == RunStatus.CANCELLED:
                break
            time.sleep(0.05)

        run = op.get_run(run_id)
        assert run.status == RunStatus.CANCELLED

        # At least some nodes should be SKIPPED
        skipped = [ns for ns in run.nodes.values() if ns.status == NodeStatus.SKIPPED]
        assert len(skipped) > 0

    def test_cancel_unknown_run_is_noop(self):
        op = self._make_operator()
        op.cancel_run("nonexistent")  # Should not raise


class TestOperatorSubscription:
    def _make_operator(self):
        return Operator(
            workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
            schedule=False,
            watch=False,
        )

    def test_subscribe_receives_updates(self):
        op = self._make_operator()
        q = op.subscribe()

        run_id = op.start_run("simple_workflow")

        # Collect updates
        updates = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                seq, run = q.get(timeout=0.1)
                updates.append((seq, run.status))
            except Exception:
                pass
            run = op.get_run(run_id)
            if run and run.status == RunStatus.SUCCESS:
                # Drain remaining
                while not q.empty():
                    seq, run = q.get_nowait()
                    updates.append((seq, run.status))
                break

        op.unsubscribe(q)

        # Should have received multiple updates with increasing sequence
        assert len(updates) >= 2
        sequences = [s for s, _ in updates]
        assert sequences == sorted(sequences), "Sequences should be monotonically increasing"

    def test_replays_exact_missed_updates_within_retained_history(self):
        op = Operator([], watch=False, schedule=False, stream_history_capacity=4)
        run = RunState(run_id="run_1", flow_name="flow")
        op._runs[run.run_id] = run
        try:
            op._notify_run(run)
            run.status = RunStatus.RUNNING
            op._notify_run(run)
            run.status = RunStatus.SUCCESS
            op._notify_run(run)

            assert op.subscribe(3).empty()
            replay = op.subscribe(1)

            assert [replay.get_nowait() for _ in range(2)] == [
                (2, RunState(run_id="run_1", flow_name="flow", status=RunStatus.RUNNING)),
                (3, RunState(run_id="run_1", flow_name="flow", status=RunStatus.SUCCESS)),
            ]
            assert replay.empty()
        finally:
            op.close()

    def test_old_cursor_recovers_latest_runs_with_fresh_ordered_sequences(self):
        op = Operator([], watch=False, schedule=False, stream_history_capacity=2)
        op._runs = {
            "run_b": RunState(run_id="run_b", flow_name="flow", status=RunStatus.SUCCESS),
            "run_a": RunState(run_id="run_a", flow_name="flow", status=RunStatus.FAILED),
        }
        try:
            for _ in range(3):
                op._notify_run(op._runs["run_a"])

            recovery = op.subscribe(0)
            updates = [recovery.get_nowait(), recovery.get_nowait()]

            assert [(seq, run.run_id) for seq, run in updates] == [
                (4, "run_a"),
                (5, "run_b"),
            ]
            assert recovery.empty()
            assert [seq for seq, _ in op._stream_history] == [4, 5]
        finally:
            op.close()

    def test_cursor_ahead_after_restart_recovers_current_runs(self):
        op = Operator([], watch=False, schedule=False)
        op._runs = {
            "run_b": RunState(run_id="run_b", flow_name="flow"),
            "run_a": RunState(run_id="run_a", flow_name="flow"),
        }
        try:
            recovery = op.subscribe(99)

            assert [recovery.get_nowait() for _ in range(2)] == [
                (1, RunState(run_id="run_a", flow_name="flow")),
                (2, RunState(run_id="run_b", flow_name="flow")),
            ]
            assert recovery.empty()
        finally:
            op.close()

    def test_subscribe_notify_race_never_misses_or_duplicates_boundary_update(self):
        for index in range(50):
            op = Operator([], watch=False, schedule=False)
            run = RunState(run_id=f"run_{index}", flow_name="flow")
            op._runs[run.run_id] = run
            barrier = threading.Barrier(3)
            subscriptions = []

            def subscribe():
                barrier.wait()
                subscriptions.append(op.subscribe(0))

            def notify():
                barrier.wait()
                op._notify_run(run)

            subscriber = threading.Thread(target=subscribe)
            publisher = threading.Thread(target=notify)
            subscriber.start()
            publisher.start()
            barrier.wait()
            subscriber.join()
            publisher.join()

            queued = []
            while not subscriptions[0].empty():
                queued.append(subscriptions[0].get_nowait())
            assert [(seq, state.run_id) for seq, state in queued] == [(1, run.run_id)]
            op.close()


class TestAgentEvidenceTransport:
    @staticmethod
    def _state():
        run = RunState(run_id="run-agent", flow_name="agent-flow")
        run.nodes["agent_1"] = NodeState(
            node_id="agent_1",
            name="agent",
            node_type="step",
            status=NodeStatus.RUNNING,
        )
        return run

    @pytest.mark.parametrize("backend", ["local", "ray"])
    def test_node_observers_keep_evidence_context_while_awaiting(self, backend):
        class Queue:
            def __init__(self):
                self.items = []

            def put(self, value):
                self.items.append(value)

        async def agent_node():
            await asyncio.sleep(0)
            emit_agent_evidence(
                {
                    "kind": "evidence",
                    "sequence": 1,
                    "event_kind": "run.started",
                    "timestamp_ns": 1,
                    "data": {"input_fields": ["query"]},
                }
            )
            return "result"

        agent_node.__agent_step__ = object()
        queue = Queue()
        if backend == "local":
            wrapped = _with_local_node_observers(
                "agent_1",
                agent_node,
                _QueueStream(queue, "operator", 20),
                _QueueStream(queue, "operator", 40),
                queue,
            )
        else:
            wrapped = _with_ray_node_observers("agent_1", agent_node, queue)

        assert asyncio.run(wrapped()) == "result"
        assert [item for item in queue.items if item.get("type") == "agent_evidence"] == [
            {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "evidence",
                    "sequence": 1,
                    "event_kind": "run.started",
                    "timestamp_ns": 1,
                    "data": {"input_fields": ["query"]},
                },
            }
        ]

    def test_operator_merges_ordered_evidence_and_final_trace(self):
        operator = Operator([], watch=False, schedule=False)
        run = self._state()
        with operator._lock:
            operator._runs[run.run_id] = run

        evidence = {
            "type": "agent_evidence",
            "node_id": "agent_1",
            "event": {
                "kind": "evidence",
                "sequence": 1,
                "event_kind": "code.generated",
                "timestamp_ns": 10,
                "data": {"iteration": 1, "code": "print('ok')"},
            },
        }
        assert operator._apply_event(run.run_id, evidence) is False
        assert operator._apply_event(run.run_id, evidence) is False
        assert (
            operator._apply_event(
                run.run_id,
                {
                    "type": "agent_evidence",
                    "node_id": "agent_1",
                    "event": {
                        "kind": "trace_finished",
                        "trace": {
                            "status": "completed",
                            "evidence": {
                                "run_id": "agent-run",
                                "complete": True,
                                "events": [],
                            },
                            "steps": [],
                        },
                    },
                },
            )
            is False
        )

        envelope = json.loads(run.nodes["agent_1"].agent_trace_json)
        assert envelope["status"] == "completed"
        assert envelope["run_id"] == "agent-run"
        assert [item["sequence"] for item in envelope["events"]] == [1]
        assert [entry.node_id for entry in run.logs] == ["agent_1", "agent_1"]
        operator.close()
