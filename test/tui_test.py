"""Core TUI interactions and state-integrity regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import datetime

import grpc
import pytest
from predict_rlm import RunTrace
from predict_rlm.trace import IterationStep, PredictCallDetail, PredictCallGroup, ToolCall
from pydantic import ValidationError

from avalanche._agent_trace import (
    AgentEvidenceMetadata,
    AgentLifecycleEvent,
    AgentTerminalDetail,
    AgentTraceEnvelope,
)
from runtime.operator.client import OperatorCallError
from runtime.operator.models import (
    AgentEvent,
    AgentEventDetailAppended,
    LogDetailAppended,
    TraceDescriptor,
    TraceDetail,
)
from tui.app import AvalancheApp
from tui.mock import INGEST_WORKFLOW, MockStateProvider
from tui.models import (
    CatalogSnapshot,
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    ResetBaseline,
    RunState,
    RunStatus,
    StreamResetNotice,
    WorkflowInfo,
)
from tui.ui_store import TraceDetailCompletion, UIStore
from tui.widgets.agent_trace import AgentOutputInspector, AgentTraceInspector
from tui.widgets.log_panel import LogWidget


async def _wait_for(pilot, predicate) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "UI did not reach the expected state"
        await pilot.pause(0.01)


def _drain_until(store: UIStore, predicate) -> None:
    deadline = time.monotonic() + 3
    while True:
        store._apply_background_updates()
        if predicate():
            return
        assert time.monotonic() < deadline, "Background update did not complete"
        time.sleep(0.005)


class _RecordingTraceProvider(MockStateProvider):
    def __init__(self) -> None:
        super().__init__(include_agent_trace=True)
        run = self.get_run("run_agent")
        assert run is not None
        node = run.nodes["inspect_agent_1"]
        envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
        self.body = AgentTerminalDetail(
            invocation_id=envelope.invocation_id,
            trace=envelope.trace,
            evidence=envelope.evidence,
        )
        self.trace_body_json = self.body.model_dump_json()
        pending = replace(
            node,
            trace=TraceDescriptor(available=True, revision=1),
            agent_trace_json=envelope.model_copy(
                update={"trace": None, "evidence": None}
            ).model_dump_json(),
        )
        second = replace(pending, node_id="second_agent_1", name="second_agent")
        workflow = self._workflows["agent_trace"]
        self._workflows = {
            workflow.selector: replace(
                workflow,
                node_ids=[pending.node_id, second.node_id],
                graph={pending.node_id: [], second.node_id: []},
                node_types={pending.node_id: "step", second.node_id: "step"},
                display_names={
                    pending.node_id: "inspect_agent",
                    second.node_id: "second_agent",
                },
                agent_node_ids=[pending.node_id, second.node_id],
            )
        }
        run = replace(
            run,
            operator_instance_id="operator-1",
            created_sequence=7,
            revision=1,
            nodes={pending.node_id: pending, second.node_id: second},
        )
        older = replace(run, run_id="older-agent-run", created_sequence=6)
        self._runs = {older.run_id: older, run.run_id: run}
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[int, Exception] = {}
        self.gates: dict[int, tuple[threading.Event, threading.Event]] = {}
        self.closed = threading.Event()
        self.trace_workers: list[threading.Thread] = []

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        return [
            replace(run, details_hydrated=False) for run in super().list_runs(workflow_selector)
        ]

    def gate(self, attempt: int) -> tuple[threading.Event, threading.Event]:
        gate = (threading.Event(), threading.Event())
        self.gates[attempt] = gate
        return gate

    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail:
        self.trace_workers.append(threading.current_thread())
        self.calls.append((run_id, node_id))
        attempt = len(self.calls)
        gate = self.gates.get(attempt)
        if gate is not None:
            entered, release = gate
            entered.set()
            release.wait()
        failure = self.failures.get(attempt)
        if failure is not None:
            raise failure
        run = self.get_run(run_id)
        assert run is not None
        descriptor = run.nodes[node_id].trace
        assert descriptor is not None
        return TraceDetail(
            operator_instance_id=run.operator_instance_id,
            run_id=run_id,
            created_sequence=run.created_sequence,
            node_id=node_id,
            descriptor_revision=descriptor.revision,
            trace_body=AgentTerminalDetail.model_validate(json.loads(self.trace_body_json)),
        )

    def close(self) -> None:
        self.closed.set()
        for _, release in self.gates.values():
            release.set()


@pytest.fixture
def store():
    workflow = WorkflowInfo(
        name="flow",
        workflow_id="flow",
        file_path="flow.py",
        node_ids=["agent"],
        graph={"agent": []},
        node_types={"agent": "step"},
        agent_node_ids=["agent"],
    )
    run = RunState(
        run_id="run-1",
        flow_name="flow",
        workflow_id="flow",
        operator_instance_id="operator-1",
        created_sequence=7,
        revision=1,
        status=RunStatus.RUNNING,
        nodes={"agent": NodeState("agent", "agent", "step", status=NodeStatus.RUNNING)},
        details_hydrated=False,
    )
    provider = MockStateProvider()
    provider._workflows = {workflow.selector: workflow}
    provider._runs = {run.run_id: run}
    state = UIStore(provider)
    try:
        _drain_until(state, lambda: state.current_run is not None)
        yield state
    finally:
        state.shutdown()


@pytest.mark.asyncio
async def test_workflow_and_node_navigation_selects_the_visible_target():
    app = AvalancheApp(workflow="ml_workflow", node="export_onnx")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.store.selected_node.name == "export_onnx_1"
        await pilot.press("down")
        assert app.store.selected_node.name == "deploy_staging_1"
        await pilot.press("right")
        assert app.store.selected_node.name == "notify_slack_1"
        await pilot.press("left")
        assert app.store.selected_node.name == "deploy_staging_1"

        await pilot.press("tab")
        assert app.store.current_workflow.name != "ml_workflow"
        assert app.store.selected_node is None
        await pilot.press("shift+tab")
        assert app.store.current_workflow.name == "ml_workflow"

        await pilot.press("space", "e")
        sidebar = app.screen.query_one("#sidebar")
        row = next(
            index
            for index, line in enumerate(sidebar.render().plain.splitlines())
            if "ingest_workflow" in line
        )
        await pilot.click("#sidebar", offset=(8, row + 1))
        await _wait_for(pilot, lambda: app.store.current_workflow.name == "ingest_workflow")
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        assert app.store.current_run.flow_name == "ingest_workflow"
        assert app.store.current_run.status is RunStatus.FAILED
        await pilot.pause()

        dag = app.screen.query_one("#dag-panel")
        row, line = next(
            (index, line)
            for index, line in enumerate(dag.render().plain.splitlines())
            if "parse" in line
        )
        await pilot.click("#dag-panel", offset=(line.index("parse") + 1, row))
        assert app.store.selected_node.name == "parse_1"
        await pilot.press("escape")
        assert app.store.selected_node is None


@pytest.mark.asyncio
async def test_start_cancel_and_history_navigation_preserve_the_selected_run():
    provider = MockStateProvider()
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        original = app.store.current_run
        await pilot.press("r")
        await _wait_for(pilot, lambda: app.store.selected_run_id != original.run_id)
        started = app.store.current_run
        assert started.status is RunStatus.RUNNING
        assert provider.get_run(started.run_id).flow_name == "order_workflow"

        await pilot.press("a", "x")
        await _wait_for(pilot, lambda: app.store.current_run.status is RunStatus.CANCELLED)
        assert provider.get_run(started.run_id).status is RunStatus.CANCELLED
        assert provider.get_run(original.run_id).status is RunStatus.SUCCESS

        await pilot.press("alt+down")
        assert app.store.selected_run_id == original.run_id
        assert app.store.focused_pane == "dag"
        await pilot.pause()
        assert app.store.selected_run_id == original.run_id
        await pilot.press("alt+up")
        assert app.store.selected_run_id == started.run_id

        # Collapsing the focused DAG routes arrows to history, not a hidden pane.
        await pilot.press("d", "down")
        assert app.store.selected_run_id == original.run_id
        await pilot.press("d", "right")
        assert app.store.selected_node.name == "fetch_orders_1"


@pytest.mark.asyncio
async def test_delayed_start_cannot_steal_workflow_selection():
    entered = threading.Event()
    release = threading.Event()

    class SlowStartProvider(MockStateProvider):
        def start_run(self, workflow_selector, **kwargs):
            entered.set()
            release.wait()
            run = RunState(
                run_id="late-start", flow_name=workflow_selector, status=RunStatus.RUNNING
            )
            self._runs[run.run_id] = run
            return run.run_id

        def close(self):
            release.set()

    provider = SlowStartProvider()
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("r")
        assert await asyncio.to_thread(entered.wait, 1)
        await pilot.press("tab")
        selected = app.store.current_workflow.selector
        assert selected != "order_workflow"
        release.set()
        await _wait_for(pilot, lambda: not app.store._start_run_in_flight)
        assert app.store.current_workflow.selector == selected
        assert app.store.selected_run_id != "late-start"
        assert all(run.flow_name == selected for run in app.store.runs_for_current_workflow)


@pytest.mark.asyncio
async def test_start_failure_and_disconnect_preserve_history_and_allow_recovery():
    class FailingProvider(MockStateProvider):
        connection_label = "operator.example:7433"
        last_error = "connection refused"

        def start_run(self, workflow_selector, **kwargs):
            raise RuntimeError("run quota exhausted")

        def ping(self):
            return self.operator_reachable

    provider = FailingProvider()
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        run_id = app.store.selected_run_id
        await pilot.press("r")
        await _wait_for(pilot, lambda: bool(app.store.run_error))
        assert "run quota exhausted" in app.screen.query_one("#status-bar").render().plain
        assert app.store.selected_run_id == run_id

        provider.stream_state = "failed"
        provider.stream_error = "update stream interrupted"
        await pilot.pause()
        overlay = app.screen.query_one("#disconnect-wrapper")
        assert not overlay.has_class("visible")
        provider.operator_reachable = False
        await _wait_for(pilot, lambda: overlay.has_class("visible"))
        assert app.store.selected_run_id == run_id

        provider.operator_reachable = True
        provider.stream_state = "live"
        await _wait_for(pilot, lambda: not overlay.has_class("visible"))
        await pilot.press("right")
        assert app.store.selected_node.name == "fetch_orders_1"
        assert app.store.selected_run_id == run_id


def test_live_terminal_state_beats_delayed_history_and_keeps_an_older_run_pinned(store):
    provider = store.provider
    current = store.current_run
    older = replace(current, run_id="older", created_sequence=1, status=RunStatus.SUCCESS)
    provider._runs = {older.run_id: older, current.run_id: current}
    store._refresh_runs_cache()
    _drain_until(store, lambda: not store._runs_refresh_in_flight)
    store.switch_run(older)
    entered = threading.Event()
    release = threading.Event()
    list_runs = provider.list_runs

    def delayed_history(selector):
        snapshot = list_runs(selector)
        entered.set()
        release.wait()
        return snapshot

    provider.list_runs = delayed_history
    try:
        store._refresh_runs_cache()
        assert entered.wait(1)
        terminal = replace(current, status=RunStatus.FAILED, revision=2)
        store.enqueue_run_update(terminal)
        store._apply_background_updates()
        release.set()
        _drain_until(store, lambda: not store._runs_refresh_in_flight)
        assert store.selected_run_id == older.run_id
        assert [run.status for run in store.runs_for_current_workflow] == [
            RunStatus.SUCCESS,
            RunStatus.FAILED,
        ]
        assert store.workflow_statuses["flow"] is RunStatus.FAILED

        # An old streamed revision cannot roll terminal state back either.
        store.enqueue_run_update(current)
        store._apply_background_updates()
        store.select_prev_run()
        assert store.current_run.status is RunStatus.FAILED
    finally:
        release.set()
        provider.list_runs = list_runs


def test_live_details_survive_summary_updates_and_repair_missing_history(store):
    run = store.current_run
    store.select_node(store.all_nodes[0])
    log = LogEntry(datetime(2026, 7, 22), LogLevel.INFO, "agent", "first detail")
    detail = LogDetailAppended(
        operator_instance_id=run.operator_instance_id,
        run_id=run.run_id,
        created_sequence=run.created_sequence,
        sequence=2,
        log_sequence=1,
        log=log,
    )
    event_json = json.dumps(
        {
            "invocation_id": "invocation",
            "timestamp_ns": 1,
            "sequence": 1,
            "event_kind": "code.executed",
            "data": {"output": "done"},
        }
    )
    store.enqueue_detail_update(detail)
    store.enqueue_detail_update(detail)
    store.enqueue_detail_update(
        AgentEventDetailAppended(
            operator_instance_id=run.operator_instance_id,
            run_id=run.run_id,
            created_sequence=run.created_sequence,
            sequence=3,
            node_id="agent",
            event=AgentEvent("invocation", 1, event_json, len(event_json)),
        )
    )
    store.enqueue_run_update(replace(run, revision=4, latest_log_sequence=1))
    store._apply_background_updates()
    assert [entry.message for entry in store.logs] == ["first detail"]
    assert store.selected_agent_events[0].data["output"] == "done"

    # Missing sequence two is recovered by a snapshot, not silently skipped.
    recovered_logs = [
        log,
        replace(log, message="missing detail"),
        replace(log, message="last detail"),
    ]
    recovered = replace(
        store.current_run,
        revision=5,
        latest_log_sequence=3,
        logs=recovered_logs,
        status=RunStatus.SUCCESS,
        details_hydrated=True,
    )
    store.provider._runs[run.run_id] = recovered
    store.enqueue_detail_update(
        replace(detail, sequence=5, log_sequence=3, log=recovered_logs[2])
    )
    _drain_until(store, lambda: len(store.logs) == 3)
    assert [entry.message for entry in store.logs] == [
        "first detail",
        "missing detail",
        "last detail",
    ]
    assert store.current_run.status is RunStatus.SUCCESS

    # Reused run IDs from another operator or incarnation must not contaminate logs.
    for foreign in (
        replace(detail, operator_instance_id="old-operator", log_sequence=4),
        replace(detail, created_sequence=6, log_sequence=4),
    ):
        store.enqueue_detail_update(foreign)
    store._apply_background_updates()
    assert [entry.message for entry in store.logs] == [
        "first detail",
        "missing detail",
        "last detail",
    ]


def test_restart_baseline_replaces_selection_before_stale_catalog_can_return(store):
    provider = store.provider
    store.switch_run(store.current_run)
    store.select_node(store.all_nodes[0])
    entered = threading.Event()
    release = threading.Event()
    old_workflows = list(store.workflows)

    def delayed_catalog():
        entered.set()
        release.wait()
        return old_workflows

    recovered = RunState("recovered", INGEST_WORKFLOW.name, status=RunStatus.SUCCESS)
    provider.list_workflows = delayed_catalog
    provider.load_reset_baseline = lambda notice: ResetBaseline(
        generation=notice.generation,
        operator_instance_id="restarted",
        as_of_event_ulid="00000000000000000000000002",
        catalog=CatalogSnapshot(workflows=(INGEST_WORKFLOW,)),
        runs_by_workflow={INGEST_WORKFLOW.selector: (recovered,)},
    )
    store._reset_baseline_loader = provider.load_reset_baseline
    try:
        store._refresh_workflow_catalog()
        assert entered.wait(1)
        provider.stream_state = "reset_required"
        for callback in provider._stream_reset_callbacks:
            callback(
                StreamResetNotice(
                    generation=1,
                    previous_event_ulid="00000000000000000000000099",
                    observed_event_ulid="00000000000000000000000002",
                    operator_instance_id="restarted",
                )
            )
        _drain_until(store, lambda: provider.stream_state == "live")
        release.set()
        _drain_until(store, lambda: not store._catalog_refresh_in_flight)
        assert store.workflows == [INGEST_WORKFLOW]
        assert store.selected_run_id == "recovered"
        assert not store.run_pinned
        assert store.selected_node is None
        assert store.logs == []
    finally:
        release.set()


@pytest.mark.asyncio
async def test_streamed_log_replacement_is_rendered_without_stale_rows():
    provider = MockStateProvider()
    run = provider.list_runs("order_workflow")[0]
    log = LogEntry(datetime(2026, 7, 22), LogLevel.INFO, "fetch_orders_1", "old result")
    run.logs = [log]
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        widget = app.screen.query_one("#log-content", LogWidget)
        await _wait_for(
            pilot, lambda: "old result" in "\n".join(line.text for line in widget.lines)
        )
        replacement = replace(
            run,
            revision=2,
            latest_log_sequence=1,
            logs=[replace(log, message="corrected result")],
        )
        provider._runs[run.run_id] = replacement
        for callback in provider._run_callbacks:
            callback(replacement)
        await _wait_for(
            pilot, lambda: "corrected result" in "\n".join(line.text for line in widget.lines)
        )
        assert "old result" not in "\n".join(line.text for line in widget.lines)


@pytest.mark.asyncio
async def test_agent_drilldown_separates_trace_output_and_returns_to_logs():
    app = AvalancheApp(workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        summary = (
            app.screen.query_one("#agent-trace-content", AgentTraceInspector).render().plain
        )
        assert "agent-mock" in summary
        await pilot.press("escape")
        logs = [entry.message for entry in app.store.logs]
        await pilot.press("enter", "enter", "down", "enter")
        trace = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        assert "Filter active records" in trace.render().plain
        await pilot.press("down", "enter")
        assert "records =" in trace.render().plain
        await pilot.press("down", "enter", "o")
        assert "Ada Lovelace" in trace.render().plain

        await pilot.press("right", "enter")
        output = app.screen.query_one("#agent-output-content", AgentOutputInspector)
        rendered = output.render().plain
        assert '"active_count": 1' in rendered
        assert '"ready": false' in rendered
        assert "SANDBOX_STDOUT_SENTINEL" not in rendered
        await pilot.press("left")
        assert "Ada Lovelace" in trace.render().plain
        await pilot.press("escape")
        assert not app.store.trace_inspector_open
        assert app.store.selected_node.name == "inspect_agent_1"
        assert [entry.message for entry in app.store.logs] == logs


@pytest.mark.asyncio
async def test_delayed_agent_trace_cannot_overwrite_newer_failure_and_recovers():
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = json.loads(node.agent_trace_json)
    body = AgentTerminalDetail.model_validate(
        {
            "invocation_id": envelope["invocation_id"],
            "trace": envelope["trace"],
            "evidence": envelope["evidence"],
        }
    )
    pending = replace(
        node,
        trace=TraceDescriptor(available=True, revision=1),
        agent_trace_json=json.dumps({**envelope, "trace": None, "evidence": None}),
    )
    run = replace(run, revision=1, nodes={node.node_id: pending})
    provider._runs[run.run_id] = run
    entered = threading.Event()
    release = threading.Event()
    fresh_entered = threading.Event()
    fresh_release = threading.Event()

    list_runs = provider.list_runs

    def list_run_summaries(selector):
        # Like gRPC listings, these do not contain the separately fetched trace body.
        return [replace(item, details_hydrated=False) for item in list_runs(selector)]

    def hydrate_trace(run_id, node_id):
        current = provider.get_run(run_id)
        revision = current.nodes[node_id].trace.revision
        if revision == 1:
            entered.set()
            release.wait()
        else:
            fresh_entered.set()
            fresh_release.wait()
        return TraceDetail(
            operator_instance_id=current.operator_instance_id,
            run_id=run_id,
            created_sequence=current.created_sequence,
            node_id=node_id,
            descriptor_revision=revision,
            trace_body=body,
        )

    def close():
        release.set()
        fresh_release.set()

    provider.list_runs = list_run_summaries
    provider.hydrate_trace = hydrate_trace
    provider.close = close
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        assert await asyncio.to_thread(entered.wait, 1)
        failed_log = LogEntry(
            datetime(2026, 7, 22), LogLevel.ERROR, node.node_id, "new failure"
        )
        failed = replace(
            run,
            revision=2,
            status=RunStatus.FAILED,
            latest_log_sequence=1,
            logs=[failed_log],
            nodes={
                node.node_id: replace(
                    pending, status=NodeStatus.FAILED, trace=replace(pending.trace, revision=2)
                )
            },
        )
        provider._runs[run.run_id] = failed
        app.store.enqueue_run_update(failed)
        await _wait_for(pilot, lambda: app.store.current_run.status is RunStatus.FAILED)
        release.set()
        await _wait_for(pilot, fresh_entered.is_set)
        assert app.store.selected_agent_trace_envelope.trace is None
        assert [entry.message for entry in app.store.logs] == ["new failure"]

        fresh_release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.trace is not None
        )
        # Force a history refresh after hydration rather than relying on the timer.
        _drain_until(app.store, lambda: not app.store._runs_refresh_in_flight)
        app.store._refresh_runs_cache()
        _drain_until(app.store, lambda: not app.store._runs_refresh_in_flight)
        hydrated = app.store.selected_agent_trace_envelope
        assert hydrated.trace == body.trace
        assert hydrated.evidence == body.evidence
        assert app.store.selected_agent_outputs == {
            "summary": {"active_count": 1, "ready": False},
            "labels": ["reviewed"],
            "note": None,
        }
        assert app.store.current_run.status is RunStatus.FAILED
        assert app.store.current_run.nodes[node.node_id].status is NodeStatus.FAILED
        assert [entry.message for entry in app.store.logs] == ["new failure"]
        await pilot.press("e")
        assert (
            body.trace.steps[0].reasoning
            in app.screen.query_one("#agent-trace-content").render().plain
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED]
)
async def test_trace_transport_failure_retries_after_backoff_without_duplicate_fetches(status):
    provider = _RecordingTraceProvider()
    provider.failures[1] = OperatorCallError(status, "temporary connection failure")
    first_entered, first_release = provider.gate(1)
    retry_entered, retry_release = provider.gate(2)
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    now = [100.0]
    app._trace_hydration_now = lambda: now[0]
    key = ("run_agent", "inspect_agent_1", 1)
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        assert await asyncio.to_thread(first_entered.wait, 1)
        for _ in range(3):
            app._hydrate_selected_trace()
        first_release.set()
        await _wait_for(pilot, lambda: key in app._trace_hydration_retry)
        assert not app._trace_hydration_attempts
        assert not app._trace_hydration_in_flight
        retry_deadline = app._trace_hydration_retry[key][1]
        assert retry_deadline > now[0]
        assert app.store.selected_agent_trace_envelope.trace is None

        now[0] = retry_deadline - 0.001
        app._hydrate_selected_trace()
        await pilot.pause(0.05)
        assert provider.calls == [key[:2]]
        now[0] = retry_deadline
        app._hydrate_selected_trace()
        assert await asyncio.to_thread(retry_entered.wait, 1)
        for _ in range(3):
            app._hydrate_selected_trace()
        retry_release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.trace == provider.body.trace
        )
        app._hydrate_selected_trace()
        await pilot.pause(0.05)
        assert provider.calls == [key[:2], key[:2]]
        assert not app._trace_hydration_attempts
        assert not app._trace_hydration_in_flight
        assert key not in app._trace_hydration_retry
        await pilot.press("e")
        inspector = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        assert provider.body.trace.steps[0].reasoning in inspector.render().plain


@pytest.mark.asyncio
async def test_failed_trace_fetch_releases_changed_node_and_run_contexts():
    provider = _RecordingTraceProvider()
    provider.failures[1] = OperatorCallError(grpc.StatusCode.UNAVAILABLE, "disconnected")
    entered, release = provider.gate(1)
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    app._trace_hydration_now = lambda: 100.0
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        assert await asyncio.to_thread(entered.wait, 1)
        app.select_node(
            next(node for node in app.store.all_nodes if node.name == "second_agent_1")
        )
        await pilot.press("enter")
        app._hydrate_selected_trace()
        assert provider.calls == [("run_agent", "inspect_agent_1")]
        release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.trace == provider.body.trace
        )
        assert provider.calls == [
            ("run_agent", "inspect_agent_1"),
            ("run_agent", "second_agent_1"),
        ]

        await pilot.press("alt+down")
        await _wait_for(pilot, lambda: app.store.selected_run_id == "older-agent-run")
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.trace == provider.body.trace
        )
        assert provider.calls == [
            ("run_agent", "inspect_agent_1"),
            ("run_agent", "second_agent_1"),
            ("older-agent-run", "second_agent_1"),
        ]
        assert not app._trace_hydration_attempts
        assert not app._trace_hydration_in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_json", "failure", "error_type"),
    [
        ("{", None, json.JSONDecodeError),
        ('{"invocation_id": "agent-mock", "trace": null}', None, ValidationError),
        (
            None,
            OperatorCallError(grpc.StatusCode.DATA_LOSS, "trace chunk offset is invalid"),
            OperatorCallError,
        ),
        (
            None,
            ValueError("trace checksum mismatch"),
            ValueError,
        ),
        (
            None,
            OperatorCallError(grpc.StatusCode.PERMISSION_DENIED, "trace access denied"),
            OperatorCallError,
        ),
        (
            None,
            RuntimeError("UNAVAILABLE is not a transport status"),
            RuntimeError,
        ),
    ],
)
async def test_invalid_trace_detail_surfaces_in_textual_after_attempt_cleanup(
    body_json, failure, error_type
):
    provider = _RecordingTraceProvider()
    if body_json is not None:
        provider.trace_body_json = body_json
    if failure is not None:
        provider.failures[1] = failure
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    teardown_workers: list[threading.Thread] = []

    def wait_for_provider_close() -> bool:
        teardown_workers.append(threading.current_thread())
        return provider.closed.wait(5)

    with pytest.raises(error_type):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for(pilot, lambda: app.store.current_run is not None)
            pending_work = [
                app._run_control_executor.submit(wait_for_provider_close),
                app.store._detail_hydration_executor.submit(wait_for_provider_close),
            ]
            await pilot.press("enter")
            await asyncio.wait_for(app._exception_event.wait(), 1)
            assert not app._trace_hydration_attempts
            assert not app._trace_hydration_in_flight
            assert not app._trace_hydration_retry
    assert provider.calls == [("run_agent", "inspect_agent_1")]
    assert provider.closed.is_set()
    assert all(work.done() and work.result() for work in pending_work)
    assert all(not worker.is_alive() for worker in provider.trace_workers + teardown_workers)
    assert app.store._shutdown.is_set()


@pytest.mark.parametrize("pending_invocation_id", [None, "call-b"])
@pytest.mark.asyncio
async def test_terminal_hydration_uses_body_invocation_for_turn_totals(
    pending_invocation_id,
):
    provider = _RecordingTraceProvider()
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    prototype = provider.body.trace.steps[0]
    events = []
    steps = []
    for invocation_id, tool_count, predict_count in [("call-a", 2, 3), ("call-b", 7, 11)]:
        step = prototype.model_copy(
            update={
                "iteration": 1,
                "code": f"print('{invocation_id}')",
                "tool_calls": [],
                "predict_calls": [],
            }
        )
        steps.append(step)
        events.append(
            AgentLifecycleEvent(
                invocation_id=invocation_id,
                sequence=1,
                timestamp_ns=1,
                event_kind="iteration.recorded",
                data={
                    "tool_count": tool_count,
                    "predict_count": predict_count,
                    "step": step.model_dump(mode="json"),
                },
            )
        )
    body = AgentTerminalDetail(
        invocation_id="call-a",
        trace=provider.body.trace.model_copy(update={"steps": steps[:1]}),
        evidence=provider.body.evidence,
    )
    provider.trace_body_json = body.model_dump_json()
    provider._runs[run.run_id] = replace(
        run,
        nodes={
            **run.nodes,
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"invocation_id": pending_invocation_id, "events": events}
                ).model_dump_json(),
            ),
        },
    )
    entered, release = provider.gate(1)
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        assert await asyncio.to_thread(entered.wait, 1)
        assert app.store.selected_agent_trace_envelope.invocation_id == pending_invocation_id
        release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.trace is not None
        )
        hydrated = app.store.selected_agent_trace_envelope
        assert hydrated.invocation_id == "call-a"
        assert hydrated.trace == body.trace
        assert hydrated.events == events
        assert app.store.selected_agent_events == events
        [turn] = app.store.selected_agent_turns
        assert (turn.invocation_id, turn.step.iteration) == ("call-a", 1)
        assert (turn.tool_count, turn.predict_count) == (2, 3)
        assert turn.step.tool_calls == []
        assert turn.step.predict_calls == []
        rendered = (
            app.screen.query_one("#agent-trace-content", AgentTraceInspector).render().plain
        )
        assert "2 tool · 3 predict" in rendered
        assert "7 tool · 11 predict" not in rendered
        assert "0 tool" not in rendered
        assert "0 predict" not in rendered


@pytest.mark.parametrize("pending_invocation_id", [None, "stale-run"])
@pytest.mark.parametrize("with_trace", [False, True])
def test_terminal_hydration_keeps_existing_history(store, with_trace, pending_invocation_id):
    run = store.current_run
    node = run.nodes["agent"]
    events = [
        AgentLifecycleEvent(
            invocation_id="sdk-run",
            sequence=1,
            timestamp_ns=1,
            event_kind="run.succeeded",
            data={"outputs": {"ready": False}},
        )
    ]
    node.status = NodeStatus.FAILED
    node.trace = TraceDescriptor(available=True, revision=3)
    envelope = AgentTraceEnvelope(
        schema_version=1,
        invocation_id=pending_invocation_id,
        status="failed",
        run_id="sdk-run",
        events=events,
        trace=None,
        evidence=None,
        error="sandbox stopped",
    )
    node.agent_trace_json = envelope.model_dump_json()
    store.select_node(store.all_nodes[0])
    evidence = AgentEvidenceMetadata(run_id="sdk-run", complete=False, terminal_outcome="error")
    trace = (
        RunTrace(
            status="error",
            model="test",
            iterations=0,
            max_iterations=1,
            duration_ms=1,
        )
        if with_trace
        else None
    )
    completion = TraceDetailCompletion(
        attempt=1,
        operator_instance_id=run.operator_instance_id,
        run_id=run.run_id,
        created_sequence=run.created_sequence,
        node_id=node.node_id,
        descriptor_revision=3,
        trace_body=AgentTerminalDetail(invocation_id="sdk-run", trace=trace, evidence=evidence),
    )
    store.enqueue_trace_hydration_completion(completion)
    store._apply_background_updates()
    hydrated = store.selected_agent_trace_envelope
    assert hydrated.invocation_id == "sdk-run"
    assert hydrated.trace == trace
    assert hydrated.evidence == evidence
    assert hydrated.status == "failed"
    assert hydrated.error == "sandbox stopped"
    assert hydrated.events == events
    assert store.selected_agent_events == events
    assert store.selected_agent_outputs == {"ready": False}
    assert store.selected_agent_inspector_state == "failed"


def test_missing_envelope_fields_fail_loudly(store):
    store.select_node(store.all_nodes[0])
    store.current_run.nodes["agent"].agent_trace_json = '{"trace": null}'
    with pytest.raises(ValidationError, match="Field required"):
        _ = store.selected_agent_trace_envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_agent_evidence_without_trace_remains_visible_and_retains_outputs(failed):
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = json.loads(node.agent_trace_json)
    events = [AgentLifecycleEvent.model_validate(event) for event in envelope["events"]]
    envelope["trace"] = None
    envelope["evidence"]["complete"] = not failed
    envelope["evidence"]["terminal_outcome"] = "error" if failed else "completed"
    evidence = AgentEvidenceMetadata.model_validate(envelope["evidence"])
    envelope["evidence"] = None
    envelope["error"] = "sandbox stopped" if failed else None
    envelope["status"] = "failed" if failed else "completed"
    provider._runs[run.run_id] = replace(
        run,
        status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
        nodes={
            node.node_id: replace(
                node,
                status=NodeStatus.FAILED if failed else NodeStatus.SUCCESS,
                trace=TraceDescriptor(available=True, revision=1),
                agent_trace_json=json.dumps(envelope),
            )
        },
    )
    list_runs = provider.list_runs

    def list_run_summaries(selector):
        return [replace(item, details_hydrated=False) for item in list_runs(selector)]

    provider.list_runs = list_run_summaries
    hydrate_calls = []

    def hydrate_trace(run_id, node_id):
        hydrate_calls.append((run_id, node_id))
        return TraceDetail(
            operator_instance_id=run.operator_instance_id,
            run_id=run_id,
            created_sequence=run.created_sequence,
            node_id=node_id,
            descriptor_revision=1,
            trace_body=AgentTerminalDetail(
                invocation_id=envelope["invocation_id"], trace=None, evidence=evidence
            ),
        )

    provider.hydrate_trace = hydrate_trace
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.evidence == evidence
        )
        app._hydrate_selected_trace()
        app._hydrate_selected_trace()
        await pilot.pause(0.05)
        assert hydrate_calls == [(run.run_id, node.node_id)]
        trace = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        rendered = trace.render().plain
        assert evidence.run_id in rendered
        assert evidence.terminal_outcome in rendered
        assert ("incomplete" if failed else "complete") in rendered
        if failed:
            assert "sandbox stopped" in rendered
        else:
            assert app.store.selected_agent_inspector_state == "completed_with_output"
        assert app.store.selected_agent_events == events
        await pilot.press("right", "enter")
        output = (
            app.screen.query_one("#agent-output-content", AgentOutputInspector).render().plain
        )
        assert '"active_count": 1' in output
        assert '"ready": false' in output
        assert "SANDBOX_STDOUT_SENTINEL" not in output


@pytest.mark.asyncio
async def test_live_execution_remains_inspectable_before_iteration_record():
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    generated = envelope.events[0]
    envelope = envelope.model_copy(
        update={
            "status": "running",
            "trace": None,
            "evidence": None,
            "events": [generated],
        }
    )
    provider._runs[run.run_id] = replace(
        run,
        status=RunStatus.RUNNING,
        nodes={
            node.node_id: replace(
                node,
                status=NodeStatus.RUNNING,
                agent_trace_json=envelope.model_dump_json(),
            ),
        },
    )
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter", "e")
        inspector = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        assert "awaiting execution" in inspector.render().plain
        assert "records = [item" in inspector.render().plain
        assert app.store.selected_agent_turns == []
        executed = AgentLifecycleEvent(
            invocation_id=generated.invocation_id,
            sequence=2,
            timestamp_ns=2,
            event_kind="code.executed",
            data={"iteration": 1, "output": "LIVE_EXECUTION_RESULT", "final": False},
        )
        event_json = executed.model_dump_json()
        app.store.enqueue_detail_update(
            AgentEventDetailAppended(
                operator_instance_id=run.operator_instance_id,
                run_id=run.run_id,
                created_sequence=run.created_sequence,
                sequence=2,
                node_id=node.node_id,
                event=AgentEvent(generated.invocation_id, 2, event_json, len(event_json)),
            )
        )
        await _wait_for(pilot, lambda: "awaiting iteration record" in inspector.render().plain)
        await pilot.press("down", "down", "e")
        assert "LIVE_EXECUTION_RESULT" in inspector.render().plain
        assert app.store.selected_agent_turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_interleaved_live_invocations_keep_independent_bodies_and_navigation(terminal):
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    step = envelope.trace.steps[0]
    generated = [
        AgentLifecycleEvent(
            invocation_id=invocation_id,
            sequence=1,
            timestamp_ns=index + 1,
            event_kind="code.generated",
            data={"iteration": step.iteration, "code": f"print('{invocation_id}_CODE')"},
        )
        for index, invocation_id in enumerate(("CALL_A", "CALL_B"))
    ]
    envelope = envelope.model_copy(
        update={
            "invocation_id": None,
            "status": "completed" if terminal else "running",
            "trace": None,
            "evidence": None,
            "events": generated[:1],
        }
    )
    provider._runs[run.run_id] = replace(
        run,
        status=RunStatus.SUCCESS if terminal else RunStatus.RUNNING,
        nodes={
            node.node_id: replace(
                node,
                status=NodeStatus.SUCCESS if terminal else NodeStatus.RUNNING,
                trace=None,
                agent_trace_json=envelope.model_dump_json(),
            ),
        },
    )
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(140, 80)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter", "e")
        inspector = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        assert "CALL_A_CODE" in inspector.render().plain

        async def append(event, transport_sequence):
            event_json = event.model_dump_json()
            app.store.enqueue_detail_update(
                AgentEventDetailAppended(
                    operator_instance_id=run.operator_instance_id,
                    run_id=run.run_id,
                    created_sequence=run.created_sequence,
                    sequence=transport_sequence,
                    node_id=node.node_id,
                    event=AgentEvent(
                        event.invocation_id,
                        transport_sequence,
                        event_json,
                        len(event_json),
                    ),
                )
            )
            await _wait_for(pilot, lambda: event in app.store.selected_agent_events)

        async def select(path):
            paths = app.store.trace_inspector_navigation_paths()
            if app.store.trace_selected_path not in paths:
                await pilot.press("down")
            offset = paths.index(path) - paths.index(app.store.trace_selected_path)
            await pilot.press(*(["down" if offset > 0 else "up"] * abs(offset)))
            assert app.store.trace_selected_path == path

        a_code = ("live", "CALL_A", "1")
        b_code = ("live", "CALL_B", "1")
        await append(generated[1], 2)
        assert app.store.trace_inspector_navigation_paths() == [
            a_code,
            a_code + ("code",),
            b_code,
            b_code + ("code",),
        ]
        rendered = inspector.render().plain
        assert "CALL_A_CODE" in rendered
        assert "CALL_B_CODE" not in rendered
        await select(b_code)
        await pilot.press("e")
        rendered = inspector.render().plain
        assert "CALL_A_CODE" in rendered
        assert "CALL_B_CODE" in rendered
        await select(a_code)
        await pilot.press("z")
        rendered = inspector.render().plain
        assert "CALL_A_CODE" not in rendered
        assert "CALL_B_CODE" in rendered
        await pilot.press("e")
        rendered = inspector.render().plain
        assert "CALL_A_CODE" in rendered
        assert "CALL_B_CODE" in rendered

        for transport_sequence, invocation_id in enumerate(("CALL_A", "CALL_B"), start=3):
            await append(
                AgentLifecycleEvent(
                    invocation_id=invocation_id,
                    sequence=2,
                    timestamp_ns=transport_sequence,
                    event_kind="code.executed",
                    data={
                        "iteration": step.iteration,
                        "output": f"{invocation_id}_OUTPUT",
                        "final": False,
                    },
                ),
                transport_sequence,
            )
            await select(("live", invocation_id, "2"))
            await pilot.press("e")
        rendered = inspector.render().plain
        assert "CALL_A_OUTPUT" in rendered
        assert "CALL_B_OUTPUT" in rendered
        await select(("live", "CALL_A", "2", "output"))
        await pilot.press("enter")
        rendered = inspector.render().plain
        assert "CALL_A_OUTPUT" not in rendered
        assert "CALL_B_OUTPUT" in rendered
        await pilot.press("enter")
        rendered = inspector.render().plain
        assert "CALL_A_OUTPUT" in rendered
        assert "CALL_B_OUTPUT" in rendered

        recorded_step = step.model_copy(
            update={"code": generated[0].data["code"], "output": "CALL_A_OUTPUT"}
        )
        await append(
            AgentLifecycleEvent(
                invocation_id="CALL_A",
                sequence=3,
                timestamp_ns=5,
                event_kind="iteration.recorded",
                data={
                    "step": recorded_step.model_dump(mode="json"),
                    "tool_count": 0,
                    "predict_count": 0,
                },
            ),
            5,
        )
        assert app.store.trace_inspector_navigation_paths() == [
            ("turn", "0"),
            b_code,
            b_code + ("code",),
            ("live", "CALL_B", "2"),
            ("live", "CALL_B", "2", "output"),
        ]
        rendered = inspector.render().plain
        assert "CALL_A_CODE" not in rendered
        assert "CALL_A_OUTPUT" not in rendered
        assert "CALL_B_CODE" in rendered
        assert "CALL_B_OUTPUT" in rendered
        await select(b_code)
        await pilot.press("z")
        rendered = inspector.render().plain
        assert "CALL_B_CODE" not in rendered
        assert "CALL_B_OUTPUT" in rendered


def test_live_append_does_not_leak_between_stores_parsing_identical_json(store):
    run = store.current_run
    generated = AgentLifecycleEvent(
        invocation_id="isolated-run",
        sequence=1,
        timestamp_ns=1,
        event_kind="code.generated",
        data={"iteration": 1, "code": "print('isolated')"},
    )
    envelope = AgentTraceEnvelope(
        schema_version=1,
        invocation_id="isolated-run",
        status="running",
        run_id="isolated-run",
        events=[generated],
        trace=None,
        evidence=None,
        error=None,
    )
    run.nodes["agent"].agent_trace_json = envelope.model_dump_json()
    store.select_node(store.all_nodes[0])
    assert store.selected_agent_events == [generated]
    run.details_hydrated = True
    other = UIStore(store.provider)
    try:
        _drain_until(other, lambda: other.current_run is not None)
        other.select_node(other.all_nodes[0])
        assert other.selected_agent_events == [generated]
        executed = AgentLifecycleEvent(
            invocation_id="isolated-run",
            sequence=2,
            timestamp_ns=2,
            event_kind="code.executed",
            data={"iteration": 1, "output": "isolated result"},
        )
        event_json = executed.model_dump_json()
        store.enqueue_detail_update(
            AgentEventDetailAppended(
                operator_instance_id=run.operator_instance_id,
                run_id=run.run_id,
                created_sequence=run.created_sequence,
                sequence=2,
                node_id="agent",
                event=AgentEvent("isolated-run", 2, event_json, len(event_json)),
            )
        )
        store._apply_background_updates()
        assert store.selected_agent_events == [generated, executed]
        assert other.selected_agent_events == [generated]
        assert other.selected_agent_turns == []
        assert store.selected_agent_trace_envelope.events == [generated]
        assert other.selected_agent_trace_envelope.events == [generated]
    finally:
        other.shutdown()


@pytest.mark.parametrize("retained_high_watermark", [False, True])
def test_trace_completion_cannot_repair_overflow_event_history(
    store, monkeypatch, retained_high_watermark
):
    from predict_rlm import IterationStep

    from tui.ui_store import _DetailRepairWatermark

    run = store.current_run
    node = run.nodes["agent"]
    events = [
        AgentLifecycleEvent(
            invocation_id="overflow-run",
            sequence=sequence,
            timestamp_ns=sequence,
            event_kind=kind,
            data=data,
        )
        for sequence, kind, data in (
            (1, "code.generated", {"iteration": 1, "code": "print('retained')"}),
            (2, "code.executed", {"iteration": 1, "output": "retained", "final": False}),
            (
                3,
                "iteration.recorded",
                {
                    "tool_count": 0,
                    "predict_count": 0,
                    "step": IterationStep(
                        iteration=1,
                        reasoning="Inspect retained history",
                        code="print('retained')",
                        output="retained",
                        untruncated_output="retained",
                        duration_ms=1,
                    ).model_dump(mode="json"),
                },
            ),
            (4, "run.succeeded", {"outputs": {"ready": True}}),
        )
    ]
    retained = [events[0], events[2]] if retained_high_watermark else [events[0]]
    envelope = AgentTraceEnvelope(
        schema_version=1,
        invocation_id="overflow-run",
        status="completed",
        run_id="overflow-run",
        events=retained,
        trace=None,
        evidence=None,
        error=None,
    )
    node.agent_trace_json = envelope.model_dump_json()
    node.trace = TraceDescriptor(available=True, revision=3, latest_event_sequence=3)
    node.status = NodeStatus.SUCCESS
    run.details_hydrated = True
    store._remember_run_details(run)
    store.select_node(store.all_nodes[0])
    key = store._detail_key(run)
    event_key = (*key, node.node_id)
    evidence = AgentEvidenceMetadata(
        run_id="overflow-run", complete=True, terminal_outcome="completed"
    )
    baseline = replace(
        run,
        nodes={
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"events": events[:3], "evidence": evidence}
                ).model_dump_json(),
            )
        },
    )
    entered = threading.Event()
    release = threading.Event()

    def get_baseline(run_id):
        entered.set()
        release.wait()
        return baseline

    monkeypatch.setattr(store.provider, "get_run", get_baseline)

    def append(event):
        event_json = event.model_dump_json()
        return store._apply_detail_update(
            AgentEventDetailAppended(
                operator_instance_id=run.operator_instance_id,
                run_id=run.run_id,
                created_sequence=run.created_sequence,
                sequence=event.sequence,
                node_id=node.node_id,
                event=AgentEvent(
                    event.invocation_id, event.sequence, event_json, len(event_json)
                ),
            )
        )

    try:
        store._repair_stream_handoff_overflow(
            False, False, (_DetailRepairWatermark(key, node.node_id, 3),)
        )
        assert entered.wait(1)
        store.enqueue_trace_hydration_completion(
            TraceDetailCompletion(
                attempt=1,
                operator_instance_id=run.operator_instance_id,
                run_id=run.run_id,
                created_sequence=run.created_sequence,
                node_id=node.node_id,
                descriptor_revision=3,
                trace_body=AgentTerminalDetail(
                    invocation_id="overflow-run", trace=None, evidence=evidence
                ),
            )
        )
        store._apply_background_updates()
        assert store.selected_agent_trace_envelope.evidence == evidence
        assert store.selected_agent_events == retained
        assert event_key in store._invalid_agent_event_details
        requirements = store._detail_hydration_requirements[key]
        # Rendering can re-cache an incomplete envelope at the required watermark.
        assert not store._detail_requirements_satisfied_by_cache(key, requirements)
        assert not append(events[2])
        release.set()
        _drain_until(store, lambda: key not in store._detail_hydration_requirements)
        assert event_key not in store._invalid_agent_event_details
        assert store.selected_agent_events == events[:3]
        assert store.selected_agent_outputs is None
        assert append(events[3])
        assert store.selected_agent_events == events
        assert store.selected_agent_outputs == {"ready": True}
    finally:
        release.set()


@pytest.mark.asyncio
async def test_error_only_agent_metadata_keeps_metadata_and_output_navigation_usable():
    from tui.widgets.agent_trace import AgentMetadataInspector

    provider = MockStateProvider(include_agent_trace=True)
    workflow = provider._workflows["agent_trace"]
    declaration_error = "Unable to resolve inspection signature"
    provider._workflows[workflow.selector] = replace(
        workflow,
        agent_metadata_json={"inspect_agent_1": json.dumps({"error": declaration_error})},
    )
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    retained_outputs = {
        "retained_payload": {"count": 2, "ready": False},
        "audit_label": "RETAINED_OUTPUT",
    }
    events = [
        event.model_copy(update={"data": {"outputs": retained_outputs}})
        if event.event_kind == "run.succeeded"
        else event
        for event in envelope.events
    ]
    provider._runs[run.run_id] = replace(
        run,
        nodes={
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"events": events}
                ).model_dump_json(),
            )
        },
    )
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter", "left", "down", "enter")
        metadata = app.screen.query_one("#agent-metadata-content", AgentMetadataInspector)
        assert declaration_error in metadata.render().plain
        assert app.store.trace_inspector_navigation_paths() == []
        await pilot.press("left")
        assert app.store.trace_inspector_navigation_paths() == [
            ("output", "retained_payload"),
            ("output", "audit_label"),
        ]
        await pilot.press("enter")
        output = app.screen.query_one("#agent-output-content", AgentOutputInspector)
        assert '"count": 2' in output.render().plain
        assert '"ready": false' in output.render().plain
        await pilot.press("down", "enter")
        assert app.store.trace_selected_path == ("output", "audit_label")
        assert retained_outputs["audit_label"] in output.render().plain
        assert app.store.selected_agent_outputs == retained_outputs
        await pilot.press("right")
        assert declaration_error in metadata.render().plain


@pytest.mark.parametrize("metadata", [{}, {"error": None}, {"error": 7}])
def test_non_error_agent_declarations_still_require_signature(store, metadata):
    store.current_workflow.agent_metadata_json["agent"] = json.dumps(metadata)
    store.current_run.nodes["agent"].agent_trace_json = AgentTraceEnvelope(
        schema_version=1,
        invocation_id="strict-metadata-run",
        status="completed",
        run_id="strict-metadata-run",
        events=[
            AgentLifecycleEvent(
                invocation_id="strict-metadata-run",
                sequence=1,
                timestamp_ns=1,
                event_kind="run.succeeded",
                data={"outputs": {"ready": False}},
            )
        ],
        trace=None,
        evidence=None,
        error=None,
    ).model_dump_json()
    store.select_node(store.all_nodes[0])
    with pytest.raises(KeyError):
        store._metadata_inspector_values()
    store.trace_inspector_tab = "output"
    with pytest.raises(KeyError):
        store.trace_inspector_navigation_paths()


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_turn_summary_uses_executed_totals_not_retained_calls(live):
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    step = envelope.trace.steps[0].model_copy(update={"tool_calls": [], "predict_calls": []})
    event = AgentLifecycleEvent(
        invocation_id=envelope.invocation_id,
        sequence=1,
        timestamp_ns=1,
        event_kind="iteration.recorded",
        data={
            "tool_count": 12,
            "predict_count": 17,
            "step": step.model_dump(mode="json"),
        },
    )
    envelope.events = [event]
    if live:
        envelope.trace = None
        envelope.evidence = None
        envelope.status = "in_progress"
    else:
        envelope.trace.steps = [step]
    node.agent_trace_json = envelope.model_dump_json()
    node.trace = None
    node.status = NodeStatus.RUNNING if live else NodeStatus.SUCCESS
    run.status = RunStatus.RUNNING if live else RunStatus.SUCCESS
    provider._runs[run.run_id] = run
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        await pilot.pause()
        rendered = (
            app.screen.query_one("#agent-trace-content", AgentTraceInspector).render().plain
        )
        assert "12 tool" in rendered
        assert "17 predict" in rendered
        assert app.store.selected_agent_turns[0].step.tool_calls == []
        assert app.store.selected_agent_turns[0].step.predict_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_concurrent_recorded_turn_totals_stay_with_their_invocation(
    retained: bool, reverse: bool
) -> None:
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    terminal = envelope.model_copy(deep=True)
    prototype = envelope.trace.steps[0]
    counts = [("call-a", 2, 3), ("call-b", 7, 11)]
    if reverse:
        counts.reverse()
    events = []
    for invocation, tool_count, predict_count in counts:
        step = prototype.model_copy(
            update={
                "code": f"print('{invocation}')",
                "tool_calls": [
                    ToolCall(name="lookup", duration_ms=1, result=index)
                    for index in range(tool_count)
                ]
                if retained
                else [],
                "predict_calls": [
                    PredictCallGroup(
                        signature="question -> answer",
                        model="test-model",
                        calls=[
                            PredictCallDetail(
                                duration_ms=1,
                                input={"question": index},
                                output={"answer": index},
                            )
                            for index in range(predict_count)
                        ],
                    )
                ]
                if retained
                else [],
            }
        )
        events.append(
            AgentLifecycleEvent(
                invocation_id=invocation,
                sequence=1,
                timestamp_ns=1,
                event_kind="iteration.recorded",
                data={
                    "tool_count": tool_count,
                    "predict_count": predict_count,
                    "step": step.model_dump(mode="json"),
                },
            )
        )
    envelope.events = events
    envelope.invocation_id = None
    envelope.trace = None
    envelope.evidence = None
    envelope.status = "running"
    node.agent_trace_json = envelope.model_dump_json()
    node.trace = None
    node.status = NodeStatus.RUNNING
    run.status = RunStatus.RUNNING
    provider._runs[run.run_id] = run
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        inspector = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        rendered = inspector.render().plain
        first, second = counts
        first_summary = f"{first[1]} tool · {first[2]} predict"
        second_summary = f"{second[1]} tool · {second[2]} predict"
        assert first_summary in rendered
        assert second_summary in rendered
        assert rendered.index(first_summary) < rendered.index(second_summary)

        terminal.invocation_id = first[0]
        terminal.events = events
        terminal.trace.steps = [IterationStep.model_validate(events[0].data["step"])]
        current_node = app.store.current_run.nodes["inspect_agent_1"]
        current_node.agent_trace_json = terminal.model_dump_json()
        current_node.status = NodeStatus.SUCCESS
        rendered = inspector.render().plain
        assert first_summary in rendered
        assert second_summary not in rendered
