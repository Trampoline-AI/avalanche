"""Tests for Operator — workflow execution and state management."""

import os
import time
from functools import partial

import pytest

from avalanche import LocalExecutor, RayExecutor
from avalanche.operator import Operator
from avalanche.operator.models import NodeStatus, RunStatus
from avalanche.operator.scheduler import Scheduler

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
                "document_ref": {"uri": "s3://bucket/input.txt"},
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
        assert any("s3=s3://bucket/input.txt" in message for message in messages)

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
        operator = Operator(
            workflow_paths=[str(workflow_file)], schedule=False, watch=False
        )
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
    def test_deprecated_exact_executor_factories_remain_supported(
        self, factory, expected
    ):
        with pytest.warns(DeprecationWarning, match="executor_factory is deprecated"):
            operator = Operator(
                [], executor_factory=factory, watch=False, schedule=False
            )
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
    def test_deprecated_executor_factories_remain_positional(
        self, factory, expected
    ):
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
        monkeypatch.setattr(
            Operator, "_start_watcher", lambda self: calls.append("watch")
        )
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
    def test_deprecated_factory_conflicts_with_explicit_configuration(
        self, explicit_config
    ):
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
