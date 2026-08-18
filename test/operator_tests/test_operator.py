"""Tests for Operator — workflow execution and state management."""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace

import pytest

from avalanche import LocalExecutor, RayExecutor
from avalanche._agent_evidence import emit_agent_evidence
from runtime.operator import Operator
from runtime.operator import operator as operator_module
from runtime.operator.models import (
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    RunStatusChanged,
    TerminalSealAppended,
    WorkflowDiscoveryDiagnostic,
    WorkflowReloadStatus,
)
from runtime.operator.operator import (
    MAX_RUN_ID_BYTES,
    InvalidRunIdError,
    RunAlreadyExistsError,
)
from runtime.operator.run_worker import (
    _import_isolated_ray,
    _QueueStream,
    _with_local_node_observers,
    _with_ray_node_observers,
)
from runtime.operator.scheduler import Scheduler
from runtime.operator.webhooks import WebhookRoute

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
        run = op.get_run(run_id)
        assert run is not None
        assert run.triggered_at is not None

    def test_start_run_publishes_multiple_requesting_runs_before_preparation(self, monkeypatch):
        op = self._make_operator()
        release_preparation = threading.Event()
        await_prepared = op._await_prepared

        def delay_preparation(handle):
            assert release_preparation.wait(timeout=5)
            return await_prepared(handle)

        monkeypatch.setattr(op, "_await_prepared", delay_preparation)
        try:
            first_run_id = op.start_run("simple_workflow")
            second_run_id = op.start_run("simple_workflow")

            assert op.get_run(first_run_id).status == RunStatus.REQUESTING
            assert op.get_run(second_run_id).status == RunStatus.REQUESTING

            release_preparation.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                statuses = [
                    op.get_run(first_run_id).status,
                    op.get_run(second_run_id).status,
                ]
                if statuses == [RunStatus.SUCCESS, RunStatus.SUCCESS]:
                    break
                time.sleep(0.05)

            assert statuses == [RunStatus.SUCCESS, RunStatus.SUCCESS]
        finally:
            release_preparation.set()
            op.close()

    def test_custom_run_id_reservation_rejects_sequential_duplicate(self):
        op = self._make_operator()

        assert op.start_run("simple_workflow", run_id="run_reserved") == "run_reserved"
        with pytest.raises(RunAlreadyExistsError, match="already exists"):
            op.start_run("simple_workflow", run_id="run_reserved")

    def test_custom_run_id_rejects_values_above_retained_summary_limit(self):
        op = self._make_operator()
        try:
            with pytest.raises(InvalidRunIdError, match="256-byte UTF-8 limit"):
                op.start_run(
                    "simple_workflow",
                    run_id="é" * (MAX_RUN_ID_BYTES // 2 + 1),
                )
        finally:
            op.close()

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
        deadline = time.monotonic() + 10
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

    def test_refresh_unchanged_catalog_does_not_publish_update(self, tmp_path, caplog):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def unchanged():\n"
            "    return None\n"
        )
        operator = Operator(
            workflow_paths=[str(workflow_file)],
            schedule=False,
            watch=False,
        )
        try:
            initial = operator.get_catalog()

            caplog.set_level("INFO", logger="runtime.operator.operator")
            operator._refresh_workflows()

            current = operator.get_catalog()
            assert current.revision == initial.revision
            assert current.as_of_sequence == initial.as_of_sequence
            assert "Workflow reload unchanged" in caplog.text
        finally:
            operator.close()

    def test_refresh_publishes_reload_status_around_scan(self, tmp_path, monkeypatch):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def unchanged():\n"
            "    return None\n"
        )
        operator = Operator(
            workflow_paths=[str(workflow_file)],
            schedule=False,
            watch=False,
        )
        subscription = operator.subscribe_operator_updates(
            operator.operator_instance_id,
            operator.current_sequence,
        )
        real_rescan = operator._registry.rescan
        observed_during_scan = []

        def rescan(*args, **kwargs):
            observed_during_scan.append(subscription.get(timeout=2.0).update.change)
            return real_rescan(*args, **kwargs)

        monkeypatch.setattr(operator._registry, "rescan", rescan)
        try:
            operator._refresh_workflows()
            finished = subscription.get(timeout=2.0)

            assert observed_during_scan == [WorkflowReloadStatus(reloading=True)]
            assert finished.update is not None
            assert finished.update.change == WorkflowReloadStatus(reloading=False)
        finally:
            operator.close()

    def test_refresh_invalid_file_retains_descriptor_and_schedule(self, tmp_path, caplog):
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
        caplog.set_level("INFO", logger="runtime.operator.operator")
        operator._refresh_workflows()

        assert [item.workflow_id for item in operator.list_workflows()] == [
            "scheduled.py::scheduled"
        ]
        assert len(operator._scheduler.list_schedules()) == 1
        assert [item.kind for item in operator.list_diagnostics()] == ["import_error"]
        assert "Workflow reload failed; retaining catalog revision" in caplog.text
        assert "import_error" in caplog.text

    def test_reload_diagnostic_summary_bounds_complete_rendered_text(self):
        diagnostic = WorkflowDiscoveryDiagnostic(
            path="/" + ("nested/" * 1_000),
            kind="invalid_catalog",
            message="invalid catalog",
        )

        summary = operator_module._summarize_reload_diagnostics((diagnostic,))

        assert len(summary) == operator_module._RELOAD_LOG_SUMMARY_LIMIT
        assert summary.endswith("...")

    def test_refresh_reconciliation_failure_rolls_back_and_can_retry(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n" "@ava.workflow\n" "def flow():\n" "    return None\n"
        )
        operator = Operator(
            workflow_paths=[str(workflow_file)],
            webhook_port=0,
            schedule=False,
            watch=False,
        )
        previous = operator._registry.view
        real_reconcile = operator._webhooks.reconcile
        reject_candidate = True

        def reconcile(routes: dict[str, WebhookRoute]) -> None:
            nonlocal reject_candidate
            if routes and reject_candidate:
                reject_candidate = False
                raise OSError("occupied " + ("port" * 1_000))
            real_reconcile(routes)

        monkeypatch.setattr(operator._webhooks, "reconcile", reconcile)
        caplog.set_level("INFO", logger="runtime.operator.operator")
        try:
            workflow_file.write_text(
                "import avalanche as ava\n"
                "@ava.workflow(webhook=True)\n"
                "def flow():\n"
                "    return None\n"
            )
            operator._refresh_workflows()

            assert operator._registry.view is previous
            assert "Workflow reload reconciliation failed" in caplog.text
            failure_record = next(
                record
                for record in caplog.records
                if "reconciliation failed" in record.getMessage()
            )
            assert len(failure_record.getMessage()) < 2_200

            workflow_file.write_text(
                "import avalanche as ava\n"
                "@ava.workflow(cron='5 * * * *', webhook=True)\n"
                "def flow():\n"
                "    return None\n"
            )
            operator._refresh_workflows()

            assert operator.get_catalog().revision == previous.revision + 1
            assert "Workflow reload succeeded" in caplog.text
        finally:
            operator.close()

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

    def test_isolated_ray_import_disables_uv_runtime_env_hook(self, monkeypatch):
        imported = SimpleNamespace()

        def import_module(name):
            assert name == "ray"
            assert os.environ["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] == "0"
            return imported

        monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "1")
        monkeypatch.setattr(
            "runtime.operator.run_worker.importlib.import_module", import_module
        )

        assert _import_isolated_ray() is imported

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
        q = op.subscribe_operator_updates()

        run_id = op.start_run("simple_workflow")

        # Collect updates
        updates = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                envelope = q.get(timeout=0.1)
                updates.append(envelope.update)
            except Exception:
                pass
            run = op.get_run(run_id)
            if run and run.status == RunStatus.SUCCESS:
                # Drain remaining
                while not q.empty():
                    updates.append(q.get_nowait().update)
                break

        op.unsubscribe_operator_updates(q)

        # Every accepted mutation is delivered once in global sequence order.
        assert len(updates) >= 2
        sequences = [update.sequence for update in updates]
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

            terminal_replay = op.subscribe_operator_updates(op.operator_instance_id, 3)
            terminal_envelope = terminal_replay.get_nowait()
            assert terminal_replay.empty()
            assert terminal_envelope.update is not None
            assert terminal_envelope.update.sequence == 4
            assert isinstance(terminal_envelope.update.change, TerminalSealAppended)
            assert terminal_envelope.update.change.seal.terminal_status is RunStatus.SUCCESS

            replay = op.subscribe_operator_updates(op.operator_instance_id, 1)

            envelopes = [replay.get_nowait() for _ in range(3)]
            assert [item.update.sequence for item in envelopes] == [2, 3, 4]
            assert [type(item.update.change) for item in envelopes] == [
                RunStatusChanged,
                RunStatusChanged,
                TerminalSealAppended,
            ]
            assert [item.update.change.status for item in envelopes[:2]] == [
                RunStatus.RUNNING,
                RunStatus.SUCCESS,
            ]
            assert envelopes[2].update.change.seal.terminal_status is RunStatus.SUCCESS
            assert replay.empty()
        finally:
            op.close()

    def test_old_update_cursor_requires_structural_reset(self):
        op = Operator([], watch=False, schedule=False, stream_history_capacity=2)
        run = RunState(run_id="run_a", flow_name="flow")
        op._runs[run.run_id] = run
        try:
            op._notify_run(run)
            run.status = RunStatus.RUNNING
            op._notify_run(run)
            run.status = RunStatus.SUCCESS
            op._notify_run(run)

            recovery = op.subscribe_operator_updates(op.operator_instance_id, 0)
            reset = recovery.get_nowait()

            assert reset.reset_required.history_floor == 3
            assert reset.reset_required.latest_sequence == 4
            assert recovery.empty()
            assert [update.sequence for update in op._stream_history] == [3, 4]
        finally:
            op.close()

    def test_cursor_ahead_after_restart_requires_structural_reset(self):
        op = Operator([], watch=False, schedule=False)
        try:
            recovery = op.subscribe_operator_updates("previous-operator", 99)
            reset = recovery.get_nowait()

            assert reset.operator_instance_id == op.operator_instance_id
            assert reset.reset_required.history_floor == 1
            assert reset.reset_required.latest_sequence == 0
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
                subscriptions.append(op.subscribe_operator_updates(op.operator_instance_id, 0))

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
            assert [envelope.update.sequence for envelope in queued] == [1]
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
                    "invocation_id": "agent-invocation",
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
                    "invocation_id": "agent-invocation",
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
        handle = SimpleNamespace(
            cancel_event=threading.Event(),
            result_bundle=None,
            success_quiesced=False,
        )
        try:
            with operator._lock:
                operator._runs[run.run_id] = run

            evidence = {
                "type": "agent_evidence",
                "node_id": "agent_1",
                "event": {
                    "kind": "evidence",
                    "invocation_id": "agent-invocation",
                    "sequence": 1,
                    "event_kind": "iteration.recorded",
                    "timestamp_ns": 10,
                    "data": {
                        "iteration": 1,
                        "duration_ms": 12,
                        "error": False,
                        "tool_count": 1,
                        "predict_count": 1,
                        "step": {
                            "iteration": 1,
                            "reasoning": "Inspect",
                            "code": "print('ok')",
                            "output": "ok",
                            "untruncated_output": "ok",
                            "error": False,
                            "duration_ms": 12,
                            "tool_calls": [{"name": "lookup", "result": "ok"}],
                            "predict_calls": [{"signature": "Answer", "calls": [{}]}],
                            "usage": {"main": {"input_tokens": 4}},
                        },
                    },
                },
            }
            assert operator._apply_event(run.run_id, handle, evidence) is False
            assert operator._apply_event(run.run_id, handle, evidence) is False
            assert (
                operator._apply_event(
                    run.run_id,
                    handle,
                    {
                        "type": "agent_evidence",
                        "node_id": "agent_1",
                        "event": {
                            "kind": "trace_finished",
                            "invocation_id": "agent-invocation",
                            "trace": {
                                "status": "completed",
                                "model": "main",
                                "sub_model": "sub",
                                "iterations": 1,
                                "max_iterations": 4,
                                "duration_ms": 125,
                                "usage": {"main": {"input_tokens": 12}, "sub": {}},
                                "telemetry_ref": {"trace_id": "trace-1"},
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

            assert run.nodes["agent_1"].agent_trace_json is None
            assert run.logs == []

            summaries = operator.list_run_summaries()
            snapshot = operator.get_run_snapshot(
                run.run_id,
                operator_instance_id=summaries.operator_instance_id,
                as_of_sequence=summaries.as_of_sequence,
            )
            assert snapshot is not None
            node = snapshot.nodes[0]
            assert node.trace is not None
            assert node.trace.status == "completed"
            assert node.trace.available is True
            assert node.trace.complete is True
            assert node.trace.event_count == 1
            assert node.trace.header is not None
            assert node.trace.header.model == "main"
            assert node.trace.header.iterations == 1
            assert json.loads(node.trace.header.usage_json)["main"]["input_tokens"] == 12
            assert json.loads(node.trace.header.telemetry_json)["trace_id"] == "trace-1"

            events = operator.list_agent_events(page_token=node.event_page_token)
            assert [item.event_sequence for item in events.events] == [1]
            assert events.events[0].event_kind == "iteration.recorded"
            assert events.events[0].iteration == 1
            assert events.events[0].tool_count == 1
            assert events.events[0].predict_count == 1
            structured_event = json.loads(operator.read_detail(events.events[0].body_token))
            assert structured_event["data"]["step"]["reasoning"] == "Inspect"
            assert structured_event["data"]["step"]["tool_calls"][0]["name"] == "lookup"
            assert structured_event["data"]["step"]["predict_calls"][0]["signature"] == "Answer"

            finalized = operator.read_trace(
                run.run_id,
                "agent_1",
                operator_instance_id=snapshot.operator_instance_id,
                revision=node.trace.revision,
            )
            finalized_trace = json.loads(finalized.data)
            assert finalized_trace["status"] == "completed"
            assert finalized_trace["evidence"]["run_id"] == "agent-run"
            assert "steps" not in finalized_trace
            assert "events" not in finalized_trace["evidence"]

            logs = operator.list_logs(page_token=snapshot.log_page_token)
            assert len(logs.logs) == 2

            materialized = operator.get_run(run.run_id)
            assert materialized is not None
            envelope = json.loads(materialized.nodes["agent_1"].agent_trace_json)
            assert envelope["invocation_id"] == "agent-invocation"
            assert envelope["status"] == "completed"
            assert envelope["run_id"] == "agent-run"
            assert [item["sequence"] for item in envelope["events"]] == [1]
            assert envelope["trace"]["steps"][0]["reasoning"] == "Inspect"
            assert [entry.node_id for entry in materialized.logs] == [
                "agent_1",
                "agent_1",
            ]
            assert run.nodes["agent_1"].agent_trace_json is None
        finally:
            operator.close()

    def test_operator_accepts_source_sequence_restart_for_new_invocation(self):
        operator = Operator([], watch=False, schedule=False)
        run = self._state()
        handle = SimpleNamespace(
            cancel_event=threading.Event(),
            result_bundle=None,
            success_quiesced=False,
        )

        def apply(event):
            return operator._apply_event(
                run.run_id,
                handle,
                {
                    "type": "agent_evidence",
                    "node_id": "agent_1",
                    "event": event,
                },
            )

        try:
            with operator._lock:
                operator._runs[run.run_id] = run

            assert (
                apply(
                    {
                        "kind": "evidence",
                        "invocation_id": "invocation-a",
                        "sequence": 1,
                        "event_kind": "code.executed",
                        "timestamp_ns": 1,
                        "data": {"output": "first"},
                    }
                )
                is False
            )
            assert (
                apply(
                    {
                        "kind": "trace_finished",
                        "invocation_id": "invocation-a",
                        "trace": {
                            "status": "completed",
                            "evidence": {"run_id": "predict-a", "complete": True},
                        },
                    }
                )
                is False
            )
            assert (
                apply(
                    {
                        "kind": "evidence",
                        "invocation_id": "invocation-b",
                        "sequence": 1,
                        "event_kind": "code.executed",
                        "timestamp_ns": 2,
                        "data": {"output": "second"},
                    }
                )
                is False
            )
            retained_log_count = len(operator._logs[run.run_id])
            assert (
                apply(
                    {
                        "kind": "evidence",
                        "invocation_id": "invocation-b",
                        "sequence": 1,
                        "event_kind": "code.executed",
                        "timestamp_ns": 3,
                        "data": {"output": "duplicate"},
                    }
                )
                is False
            )
            assert len(operator._logs[run.run_id]) == retained_log_count
            assert (
                apply(
                    {
                        "kind": "trace_unavailable",
                        "invocation_id": "invocation-b",
                        "error": "retry failed",
                    }
                )
                is False
            )

            summaries = operator.list_run_summaries()
            snapshot = operator.get_run_snapshot(
                run.run_id,
                operator_instance_id=summaries.operator_instance_id,
                as_of_sequence=summaries.as_of_sequence,
            )
            assert snapshot is not None
            trace = snapshot.nodes[0].trace
            assert trace is not None
            assert trace.status == "unavailable"
            assert trace.event_count == 2
            assert trace.latest_event_sequence == 2

            page = operator.list_agent_events(page_token=snapshot.nodes[0].event_page_token)
            assert [(item.invocation_id, item.event_sequence) for item in page.events] == [
                ("invocation-a", 1),
                ("invocation-b", 2),
            ]
            bodies = [json.loads(operator.read_detail(item.body_token)) for item in page.events]
            assert [(body["invocation_id"], body["sequence"]) for body in bodies] == [
                ("invocation-a", 1),
                ("invocation-b", 1),
            ]

            materialized = operator.get_run(run.run_id)
            assert materialized is not None
            envelope = json.loads(materialized.nodes["agent_1"].agent_trace_json)
            assert envelope["invocation_id"] == "invocation-b"
            assert [
                (item["invocation_id"], item["sequence"]) for item in envelope["events"]
            ] == [("invocation-b", 1)]
        finally:
            operator.close()


def test_running_snapshot_reports_server_elapsed_duration(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run = RunState(run_id="run-1", flow_name="Flow")
    run.nodes["agent_1"] = NodeState(
        node_id="agent_1",
        name="Agent",
        node_type="step",
        status=NodeStatus.RUNNING,
        started_at=10.0,
    )
    monkeypatch.setattr(operator_module.time, "monotonic", lambda: 14.5)
    try:
        snapshot = operator._run_snapshot_locked(
            run,
            summary=operator._run_summary_locked(run),
            as_of_sequence=1,
        )
        assert snapshot.nodes[0].running_elapsed_seconds == 4.5
    finally:
        operator.close()


def test_prepared_run_retains_immutable_topology_after_source_metadata_changes():
    prepared = {
        "display_name": "Original",
        "node_ids": ["source_1", "step_1"],
        "graph": {"source_1": ["step_1"], "step_1": []},
        "node_types": {"source_1": "source", "step_1": "step"},
        "display_names": {"source_1": "Source", "step_1": "Step"},
        "agent_field_schemas_json": {
            "step_1": '{"inputs":[{"name":"question","type":"str","description":""}],'
            '"outputs":[]}'
        },
        "agent_instruction_lines": {"step_1": "Original instruction."},
    }

    run = Operator._run_from_prepared(
        "run-topology",
        "flow.py::original",
        "Original",
        "manual",
        1.0,
        prepared,
    )
    prepared["node_ids"].append("new_1")
    prepared["graph"]["source_1"] = ["new_1"]
    prepared["display_names"]["step_1"] = "Changed"
    prepared["agent_field_schemas_json"]["step_1"] = '{"inputs":[],"outputs":[]}'
    prepared["agent_instruction_lines"]["step_1"] = "Changed instruction."

    assert run.topology.node_ids == ("source_1", "step_1")
    assert run.topology.graph == (("source_1", ("step_1",)), ("step_1", ()))
    assert dict(run.topology.display_names) == {"source_1": "Source", "step_1": "Step"}
    assert dict(run.topology.agent_field_schemas_json) == {
        "step_1": (
            '{"inputs":[{"name":"question","type":"str","description":""}],"outputs":[]}'
        )
    }
    assert dict(run.topology.agent_instruction_lines) == {"step_1": "Original instruction."}
