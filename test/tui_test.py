"""Core TUI interactions and state-integrity regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import datetime

import pytest

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
from tui.ui_store import UIStore
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
        {"sequence": 1, "event_kind": "code.executed", "data": {"output": "done"}}
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
    assert store.selected_agent_events[0]["data"]["output"] == "done"

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
    body = json.loads(node.agent_trace_json)["trace"]
    pending = replace(
        node,
        trace=TraceDescriptor(available=True, revision=1),
        agent_trace_json=json.dumps({"trace": None}),
    )
    run = replace(run, revision=1, nodes={node.node_id: pending})
    provider._runs[run.run_id] = run
    entered = threading.Event()
    release = threading.Event()
    fresh_entered = threading.Event()
    fresh_release = threading.Event()

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
        assert app.store.selected_agent_trace_envelope["trace"] is None
        assert [entry.message for entry in app.store.logs] == ["new failure"]

        fresh_release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.get("trace") is not None
        )
        assert app.store.current_run.status is RunStatus.FAILED
        assert app.store.current_run.nodes[node.node_id].status is NodeStatus.FAILED
        assert [entry.message for entry in app.store.logs] == ["new failure"]
        await pilot.press("e")
        assert (
            "Filter active records"
            in app.screen.query_one("#agent-trace-content").render().plain
        )
