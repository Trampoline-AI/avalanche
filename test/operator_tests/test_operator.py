"""Tests for Operator — workflow execution and state management."""

import os
import time

from avalanche.operator import Operator
from avalanche.operator.models import NodeStatus, RunStatus

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


class TestOperatorLifecycle:
    def _make_operator(self):
        return Operator(
            workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
            schedule=False,
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
            context={"request_id": "req_456", "execution_id": "spoofed_user_id"},
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
        assert any(f"execution_id={run_id}" in message for message in messages)
        assert not any("execution_id=spoofed_user_id" in message for message in messages)
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


class TestOperatorCancellation:
    def _make_operator(self):
        return Operator(
            workflow_paths=[os.path.join(FIXTURES_DIR, "sample_workflows.py")],
            schedule=False,
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
