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
from predict_rlm import RunEvent, RunEventKind, RunEvidence, RunTrace
from predict_rlm.trace import IterationStep, PredictCallDetail, PredictCallGroup, ToolCall
from pydantic import ValidationError

from avalanche._agent_trace import AgentTraceEnvelope
from avalanche.agent.live import (
    AGENT_TRACE_TERMINAL,
    AgentLiveEvent,
    AgentTraceFinished,
    AgentTraceUnavailable,
)
from avalanche.agent.native.predict_rlm import IterationRecorded, PredictRLMOmissions
from avalanche.runtimes.predict_rlm.events import project_event
from avalanche.runtimes.predict_rlm.trace import trace_header
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

_MOCK_REASONING = "Filter active records and summarize their names."


def _event(invocation_id: str, sequence: int, kind: RunEventKind, data: dict) -> AgentLiveEvent:
    """One PredictRLM event as an agent step sends it, made by the real adapter."""
    native, common = project_event(RunEvent(invocation_id, sequence, kind, sequence, data))
    return AgentLiveEvent(
        invocation_id=invocation_id,
        sequence=sequence,
        timestamp_ns=sequence,
        common=common,
        native=native,
    )


def _finished(
    invocation_id: str,
    trace: RunTrace,
    *,
    run_id: str,
    complete: bool = True,
    outcome: str = "completed",
) -> AgentTraceFinished:
    header = trace_header(
        trace,
        RunEvidence(run_id=run_id, complete=complete, terminal_outcome=outcome),
        model=None,
        sub_model=None,
    )
    return AgentTraceFinished(
        invocation_id=invocation_id,
        header=header,
        outputs=None,
        omitted_outputs=(),
        complete=complete,
    )


def _unavailable(
    invocation_id: str, *, run_id: str, complete: bool, status: str
) -> AgentTraceUnavailable:
    return AgentTraceUnavailable(
        invocation_id=invocation_id,
        runtime_kind="predict_rlm",
        status=status,
        error="trace unavailable",
        external_run_id=run_id,
        complete=complete,
    )


def _step(code: str, *, tools: int = 0, predicts: int = 0) -> IterationStep:
    return IterationStep(
        iteration=1,
        reasoning=_MOCK_REASONING,
        code=code,
        output="SANDBOX_STDOUT_SENTINEL",
        untruncated_output="FULL OUTPUT",
        duration_ms=14,
        tool_calls=[
            ToolCall(name="lookup", duration_ms=1, result=index) for index in range(tools)
        ],
        predict_calls=[
            PredictCallGroup(
                signature="question -> answer",
                model="test-model",
                calls=[
                    PredictCallDetail(
                        duration_ms=1, input={"question": index}, output={"answer": index}
                    )
                    for index in range(predicts)
                ],
            )
        ]
        if predicts
        else [],
    )


def _without_retained_calls(event: AgentLiveEvent) -> AgentLiveEvent:
    """A turn whose calls were dropped for size; the omitted counts keep its totals."""
    turn = event.common.turn
    predicts = len(turn.native.predict_calls)
    native_turn = turn.native.model_copy(update={"predict_calls": ()})
    return event.model_copy(
        update={
            "common": event.common.model_copy(
                update={
                    "turn": turn.model_copy(update={"tool_calls": (), "native": native_turn}),
                    "omitted_tool_calls": len(turn.tool_calls),
                }
            ),
            # A turn that left calls out carries PredictRLM's omission counts.
            "native": IterationRecorded(
                omissions=PredictRLMOmissions(
                    reason="iteration detail exceeds byte limit",
                    tool_count=len(turn.tool_calls) - predicts,
                    predict_count=predicts,
                    predict_group_count=0,
                )
            ),
        }
    )


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
        self.body = envelope.terminal
        self.trace_body_json = self.body.model_dump_json()
        pending = replace(
            node,
            trace=TraceDescriptor(available=True, revision=1),
            agent_trace_json=envelope.model_copy(update={"terminal": None}).model_dump_json(),
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
            trace_body=AGENT_TRACE_TERMINAL.validate_json(self.trace_body_json),
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
    event_json = _event(
        "invocation", 1, RunEventKind.CODE_EXECUTED, {"iteration": 1, "output": "done"}
    ).model_dump_json()
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
    assert store.selected_agent_events[0].native.output == "done"

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
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    body = envelope.terminal
    pending = replace(
        node,
        trace=TraceDescriptor(available=True, revision=1),
        agent_trace_json=envelope.model_copy(update={"terminal": None}).model_dump_json(),
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
        assert app.store.selected_agent_trace_envelope.terminal is None
        assert [entry.message for entry in app.store.logs] == ["new failure"]

        fresh_release.set()
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal is not None
        )
        # Force a history refresh after hydration rather than relying on the timer.
        _drain_until(app.store, lambda: not app.store._runs_refresh_in_flight)
        app.store._refresh_runs_cache()
        _drain_until(app.store, lambda: not app.store._runs_refresh_in_flight)
        hydrated = app.store.selected_agent_trace_envelope
        assert hydrated.terminal == body
        assert app.store.selected_agent_outputs == {
            "summary": {"active_count": 1, "ready": False},
            "labels": ["reviewed"],
            "note": None,
        }
        assert app.store.current_run.status is RunStatus.FAILED
        assert app.store.current_run.nodes[node.node_id].status is NodeStatus.FAILED
        assert [entry.message for entry in app.store.logs] == ["new failure"]
        await pilot.press("e")
        assert _MOCK_REASONING in app.screen.query_one("#agent-trace-content").render().plain


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
        assert app.store.selected_agent_trace_envelope.terminal is None

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
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal == provider.body
        )
        app._hydrate_selected_trace()
        await pilot.pause(0.05)
        assert provider.calls == [key[:2], key[:2]]
        assert not app._trace_hydration_attempts
        assert not app._trace_hydration_in_flight
        assert key not in app._trace_hydration_retry
        await pilot.press("e")
        inspector = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        assert _MOCK_REASONING in inspector.render().plain


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
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal == provider.body
        )
        assert provider.calls == [
            ("run_agent", "inspect_agent_1"),
            ("run_agent", "second_agent_1"),
        ]

        await pilot.press("alt+down")
        await _wait_for(pilot, lambda: app.store.selected_run_id == "older-agent-run")
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal == provider.body
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
        ("{", None, ValidationError),
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
    steps = [
        _step(f"print('{invocation_id}')", tools=tools, predicts=predicts)
        for invocation_id, tools, predicts in [("call-a", 2, 3), ("call-b", 7, 11)]
    ]
    events = [
        _event(invocation_id, 1, RunEventKind.ITERATION_RECORDED, {"step": step})
        for invocation_id, step in zip(("call-a", "call-b"), steps, strict=True)
    ]
    body = _finished(
        "call-a",
        RunTrace(
            status="completed",
            model="mock-main",
            iterations=1,
            max_iterations=4,
            duration_ms=20,
            steps=steps[:1],
        ),
        run_id="agent-mock",
    )
    provider.trace_body_json = body.model_dump_json()
    provider._runs[run.run_id] = replace(
        run,
        nodes={
            **run.nodes,
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"invocation_id": pending_invocation_id, "events": tuple(events)}
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
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal is not None
        )
        hydrated = app.store.selected_agent_trace_envelope
        assert hydrated.invocation_id == "call-a"
        assert hydrated.terminal == body
        assert hydrated.events == tuple(events)
        assert app.store.selected_agent_events == events
        # Only the finished invocation's turns sit next to its final trace.
        [turn] = app.store.selected_agent_turns
        assert (turn.invocation_id, turn.finished.turn.index) == ("call-a", 1)
        assert turn.finished.tool_call_count == 5
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
    events = [_event("sdk-run", 1, RunEventKind.CODE_GENERATED, {"iteration": 1, "code": "x"})]
    node.status = NodeStatus.FAILED
    node.trace = TraceDescriptor(available=True, revision=3)
    envelope = AgentTraceEnvelope(
        invocation_id=pending_invocation_id,
        status="failed",
        events=tuple(events),
        terminal=None,
        error="sandbox stopped",
    )
    node.agent_trace_json = envelope.model_dump_json()
    store.select_node(store.all_nodes[0])
    terminal = (
        _finished(
            "sdk-run",
            RunTrace(
                status="error", model="test", iterations=0, max_iterations=1, duration_ms=1
            ),
            run_id="sdk-run",
            complete=False,
            outcome="error",
        )
        if with_trace
        else _unavailable("sdk-run", run_id="sdk-run", complete=False, status="failed")
    )
    completion = TraceDetailCompletion(
        attempt=1,
        operator_instance_id=run.operator_instance_id,
        run_id=run.run_id,
        created_sequence=run.created_sequence,
        node_id=node.node_id,
        descriptor_revision=3,
        trace_body=terminal,
    )
    store.enqueue_trace_hydration_completion(completion)
    store._apply_background_updates()
    hydrated = store.selected_agent_trace_envelope
    assert hydrated.invocation_id == "sdk-run"
    assert hydrated.terminal == terminal
    assert hydrated.status == "failed"
    assert hydrated.error == "sandbox stopped"
    assert hydrated.events == tuple(events)
    assert store.selected_agent_events == events
    # A failed call has no outputs; they travel only on a finished final event.
    assert store.selected_agent_outputs is None
    assert store.selected_agent_inspector_state == "failed"


def test_missing_envelope_fields_fail_loudly(store):
    store.select_node(store.all_nodes[0])
    store.current_run.nodes["agent"].agent_trace_json = '{"trace": null}'
    with pytest.raises(ValidationError, match="Field required"):
        _ = store.selected_agent_trace_envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_agent_evidence_without_trace_remains_visible(failed):
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    events = list(envelope.events)
    unavailable = _unavailable(
        envelope.invocation_id,
        run_id="agent-mock",
        complete=not failed,
        status="failed" if failed else "succeeded",
    )
    envelope = envelope.model_copy(
        update={
            "terminal": None,
            "error": "sandbox stopped" if failed else None,
            "status": "failed" if failed else "succeeded",
        }
    )
    provider._runs[run.run_id] = replace(
        run,
        status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
        nodes={
            node.node_id: replace(
                node,
                status=NodeStatus.FAILED if failed else NodeStatus.SUCCESS,
                trace=TraceDescriptor(available=True, revision=1),
                agent_trace_json=envelope.model_dump_json(),
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
            trace_body=unavailable,
        )

    provider.hydrate_trace = hydrate_trace
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter")
        await _wait_for(
            pilot, lambda: app.store.selected_agent_trace_envelope.terminal == unavailable
        )
        app._hydrate_selected_trace()
        app._hydrate_selected_trace()
        await pilot.pause(0.05)
        assert hydrate_calls == [(run.run_id, node.node_id)]
        trace = app.screen.query_one("#agent-trace-content", AgentTraceInspector)
        rendered = trace.render().plain
        assert unavailable.external_run_id in rendered
        assert f"terminal={unavailable.status}" in rendered
        assert ("incomplete" if failed else "complete") in rendered
        if failed:
            assert "sandbox stopped" in rendered
        else:
            # Outputs travel only on a finished trace, so an unavailable one has none.
            assert app.store.selected_agent_inspector_state == "completed_without_output"
        assert app.store.selected_agent_events == events
        assert app.store.selected_agent_outputs is None


@pytest.mark.asyncio
async def test_live_execution_remains_inspectable_before_iteration_record():
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    generated = envelope.events[0]
    envelope = envelope.model_copy(
        update={"status": "running", "terminal": None, "events": (generated,)}
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
        executed = _event(
            generated.invocation_id,
            2,
            RunEventKind.CODE_EXECUTED,
            {"iteration": 1, "output": "LIVE_EXECUTION_RESULT"},
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
    generated = [
        _event(
            invocation_id,
            1,
            RunEventKind.CODE_GENERATED,
            {"iteration": 1, "code": f"print('{invocation_id}_CODE')"},
        )
        for invocation_id in ("CALL_A", "CALL_B")
    ]
    envelope = envelope.model_copy(
        update={
            "invocation_id": None,
            "status": "succeeded" if terminal else "running",
            "terminal": None,
            "events": tuple(generated[:1]),
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
                _event(
                    invocation_id,
                    2,
                    RunEventKind.CODE_EXECUTED,
                    {"iteration": 1, "output": f"{invocation_id}_OUTPUT"},
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

        recorded_step = _step(generated[0].native.code).model_copy(
            update={"output": "CALL_A_OUTPUT"}
        )
        await append(
            _event("CALL_A", 3, RunEventKind.ITERATION_RECORDED, {"step": recorded_step}),
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
    generated = _event(
        "isolated-run",
        1,
        RunEventKind.CODE_GENERATED,
        {"iteration": 1, "code": "print('isolated')"},
    )
    envelope = AgentTraceEnvelope(
        invocation_id="isolated-run",
        status="running",
        events=(generated,),
        terminal=None,
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
        executed = _event(
            "isolated-run",
            2,
            RunEventKind.CODE_EXECUTED,
            {"iteration": 1, "output": "isolated result"},
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
        assert store.selected_agent_trace_envelope.events == (generated,)
        assert other.selected_agent_trace_envelope.events == (generated,)
    finally:
        other.shutdown()


@pytest.mark.parametrize("retained_high_watermark", [False, True])
def test_trace_completion_cannot_repair_overflow_event_history(
    store, monkeypatch, retained_high_watermark
):
    from tui.ui_store import _DetailRepairWatermark

    run = store.current_run
    node = run.nodes["agent"]
    step = IterationStep(
        iteration=1,
        reasoning="Inspect retained history",
        code="print('retained')",
        output="retained",
        untruncated_output="retained",
        duration_ms=1,
    )
    events = [
        _event("overflow-run", sequence, kind, data)
        for sequence, kind, data in (
            (1, RunEventKind.CODE_GENERATED, {"iteration": 1, "code": "print('retained')"}),
            (2, RunEventKind.CODE_EXECUTED, {"iteration": 1, "output": "retained"}),
            (3, RunEventKind.ITERATION_RECORDED, {"step": step}),
            (4, RunEventKind.SESSION_RELEASED, {}),
        )
    ]
    retained = [events[0], events[2]] if retained_high_watermark else [events[0]]
    envelope = AgentTraceEnvelope(
        invocation_id="overflow-run",
        status="succeeded",
        events=tuple(retained),
        terminal=None,
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
    terminal = _unavailable(
        "overflow-run", run_id="overflow-run", complete=True, status="succeeded"
    )
    baseline = replace(
        run,
        nodes={
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"events": tuple(events[:3]), "terminal": terminal}
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
                trace_body=terminal,
            )
        )
        store._apply_background_updates()
        assert store.selected_agent_trace_envelope.terminal == terminal
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
    finally:
        release.set()


@pytest.mark.asyncio
async def test_missing_agent_declaration_keeps_output_navigation_usable():
    from tui.widgets.agent_trace import AgentMetadataInspector

    provider = MockStateProvider(include_agent_trace=True)
    workflow = provider._workflows["agent_trace"]
    provider._workflows[workflow.selector] = replace(workflow, agent_declarations={})
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    retained_outputs = {
        "retained_payload": {"count": 2, "ready": False},
        "audit_label": "RETAINED_OUTPUT",
    }
    terminal = envelope.terminal.model_copy(update={"outputs": retained_outputs})
    provider._runs[run.run_id] = replace(
        run,
        nodes={
            node.node_id: replace(
                node,
                agent_trace_json=envelope.model_copy(
                    update={"terminal": terminal}
                ).model_dump_json(),
            )
        },
    )
    app = AvalancheApp(provider=provider, workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("enter", "left", "down", "enter")
        metadata = app.screen.query_one("#agent-metadata-content", AgentMetadataInspector)
        assert "not published" in metadata.render().plain
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
        assert "not published" in metadata.render().plain


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_turn_summary_uses_executed_totals_not_retained_calls(live):
    provider = MockStateProvider(include_agent_trace=True)
    run = provider.get_run("run_agent")
    node = run.nodes["inspect_agent_1"]
    envelope = AgentTraceEnvelope.model_validate_json(node.agent_trace_json)
    event = _without_retained_calls(
        _event(
            envelope.invocation_id,
            1,
            RunEventKind.ITERATION_RECORDED,
            {"step": _step("pass", tools=12, predicts=17)},
        )
    )
    envelope = envelope.model_copy(update={"events": (event,)})
    if live:
        envelope = envelope.model_copy(update={"terminal": None, "status": "in_progress"})
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
        assert app.store.selected_agent_turns[0].finished.turn.native.predict_calls == ()


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
    terminal = envelope
    counts = [("call-a", 2, 3), ("call-b", 7, 11)]
    if reverse:
        counts.reverse()
    events = []
    for invocation, tool_count, predict_count in counts:
        event = _event(
            invocation,
            1,
            RunEventKind.ITERATION_RECORDED,
            {"step": _step(f"print('{invocation}')", tools=tool_count, predicts=predict_count)},
        )
        events.append(event if retained else _without_retained_calls(event))
    envelope = envelope.model_copy(
        update={
            "events": tuple(events),
            "invocation_id": None,
            "terminal": None,
            "status": "running",
        }
    )
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

        terminal = terminal.model_copy(
            update={
                "invocation_id": first[0],
                "events": tuple(events),
                "terminal": terminal.terminal.model_copy(update={"invocation_id": first[0]}),
            }
        )
        current_node = app.store.current_run.nodes["inspect_agent_1"]
        current_node.agent_trace_json = terminal.model_dump_json()
        current_node.status = NodeStatus.SUCCESS
        rendered = inspector.render().plain
        assert first_summary in rendered
        assert second_summary not in rendered


class _ExtensionFixture:
    protocol_version = (1, 0)
    features = frozenset({"paged-resources", "json-schema-forms", "invalidations"})

    def __init__(self, provider):
        self.provider = provider
        self.callbacks = []
        self.calls = []
        self.unsubscribed = False
        self.fail_reads = False
        self.gate = None
        self.revision = "r1"

    def describe(self, context):
        from types import MappingProxyType

        from tui.state import ActionDescriptor, ExtensionDescription, PanelDescriptor

        schema = MappingProxyType(
            {
                "type": "object",
                "required": ["count"],
                "properties": {
                    "count": {"type": "integer", "default": 3, "minimum": 1},
                    "options": {"type": ["object", "null"], "default": None},
                },
                "additionalProperties": False,
            }
        )
        actions = (
            ActionDescriptor(
                "run.start",
                "Start typed run",
                "run:create",
                True,
                None,
                True,
                "Confirm exact input",
                schema,
            ),
        )
        panels = tuple(
            PanelDescriptor(
                f"fixture.{renderer}",
                f"Fixture {renderer}",
                "run",
                "fixture",
                renderer,
                ("id", "value"),
            )
            for renderer in ("table", "tree", "json", "diff")
        )
        return ExtensionDescription(
            context, self.protocol_version, self.features, self.revision, (), actions, panels
        )

    def read(self, resource, context, *, cursor=None, limit=100, byte_limit=262144):
        from types import MappingProxyType

        from tui.state import ExtensionPage, TuiExtensionError

        self.calls.append(("read", context, cursor, limit, byte_limit))
        if self.gate is not None:
            entered, release = self.gate
            entered.set()
            release.wait(3)
        if self.fail_reads:
            raise TuiExtensionError("unavailable", "Resource unavailable")
        items = () if cursor == "empty" else (MappingProxyType({"id": "row", "value": 7}),)
        return ExtensionPage(
            context, items, "empty" if cursor is None else None, False, self.revision
        )

    def invoke(self, action, context, payload, *, intent_id):
        from tui.state import ActionResult

        self.calls.append(("invoke", context, dict(payload), intent_id))
        run_id = self.provider.start_run(context.flow, **payload)
        return ActionResult(context, intent_id, "accepted", self.revision, {"run_id": run_id})

    def subscribe(self, callback):
        self.callbacks.append(callback)

        def unsubscribe():
            self.unsubscribed = True
            self.callbacks.remove(callback)

        return unsubscribe


class _RevampProvider(MockStateProvider):
    def __init__(self):
        super().__init__(include_agent_trace=True)
        self.reads = []
        self.starts = []
        self.tui_extensions = _ExtensionFixture(self)

    def list_workflows(self):
        self.reads.append("catalog")
        return super().list_workflows()

    def list_runs(self, selector):
        self.reads.append("history")
        return super().list_runs(selector)

    def get_run(self, run_id):
        self.reads.append("run")
        return super().get_run(run_id)

    def ping(self):
        self.reads.append("ping")
        return True

    def start_run(self, selector, **kwargs):
        self.starts.append((selector, kwargs))
        return "accepted-fixture-run"


@pytest.mark.asyncio
async def test_revamp_catalog_is_event_driven_without_health_or_run_polling():
    provider = _RevampProvider()
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.pause(0.2)
        before = list(provider.reads)
        await pilot.press("c")
        assert app.store.sidebar_visible
        await pilot.pause(2.2)
        assert provider.reads == before
        assert "ping" not in provider.reads
        assert app.screen.query_one("#breadcrumbs").render().plain


@pytest.mark.asyncio
async def test_revamp_history_virtualizes_ten_thousand_rows_and_uses_real_dates():
    from tui.widgets.run_history import RunHistoryWidget

    app = AvalancheApp()
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        run = app.store.current_run
        runs = [
            replace(run, run_id=f"historical-{i}", triggered_at=1577836800 + i)
            for i in range(10000)
        ]
        app.store._set_runs_cache(runs)
        app.store.switch_run(runs[-1])
        app._refresh_widgets()
        await pilot.pause()
        widget = app.screen.query_one("#run-history-content", RunHistoryWidget)
        assert widget.get_content_height(None, None, 100) == 10000
        assert len(widget.render().plain.splitlines()) <= widget.parent.size.height
        expected_date = datetime.fromtimestamp(runs[-1].triggered_at).strftime("%Y-%m-%d")
        assert expected_date in widget._row(0).plain
        await pilot.press("alt+down")
        assert app.store.selected_run_id == "historical-9998"
        app.store._set_runs_cache([])
        app._refresh_widgets()
        await pilot.pause()
        assert widget.get_content_height(None, None, 100) == 1


@pytest.mark.asyncio
async def test_revamp_large_dag_collapses_and_renders_only_the_viewport_on_resize():
    provider = _RevampProvider()
    ids = [f"n{i}" for i in range(1000)]
    flow = WorkflowInfo(
        name="large",
        file_path="large.py",
        node_ids=ids,
        graph={},
        node_types={nid: "step" for nid in ids},
    )
    provider._workflows = {flow.selector: flow}
    provider._runs = {}
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: len(app.store.all_nodes) == 1000)
        dag = app.screen.query_one("#dag-panel")
        dag.render()
        assert len(dag._large_rows) == 20
        await pilot.press("e")
        assert len(dag._large_rows) > 1000
        await pilot.press("z")
        assert len(dag._large_rows) == 20
        await pilot.resize_terminal(80, 24)
        assert len(dag.render().plain.splitlines()) <= dag.parent.size.height


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["value", "string", "str"])
@pytest.mark.parametrize("node_count", [1, 201])
async def test_provider_statuses_outside_enums_render_verbatim(representation, node_count):
    from types import SimpleNamespace

    from tui.widgets.run_detail import RunDetailWidget
    from tui.widgets.run_history import RunHistoryWidget
    from tui.widgets.sidebar import Sidebar

    class RawStatus:
        def __init__(self, label):
            self.label = label

        def __str__(self):
            return self.label

    def status(label):
        if representation == "value":
            # A provider-side status need not even be hashable.
            return SimpleNamespace(value=label)
        return label if representation == "string" else RawStatus(label)

    provider = _RevampProvider()
    run = provider.get_run("run_agent")
    workflow = provider._workflows["agent_trace"]
    prototype = run.nodes["inspect_agent_1"]
    ids = [f"node_{i}" for i in range(node_count)]
    workflow = replace(
        workflow,
        node_ids=ids,
        graph={nid: ids[i + 1 : i + 2] for i, nid in enumerate(ids)},
        node_types={nid: "step" for nid in ids},
        display_names={nid: nid for nid in ids},
        agent_node_ids=[],
    )
    run = replace(
        run,
        status=status("parked"),
        nodes={
            nid: replace(
                prototype,
                node_id=nid,
                name=nid,
                status=status("cancelled"),
                started_at=10.0,
                ended_at=12.0,
            )
            for nid in ids
        },
    )
    provider._workflows = {workflow.selector: workflow}
    provider._runs = {run.run_id: run}
    app = AvalancheApp(provider=provider, workflow=workflow.selector, node=ids[0])
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("c")
        dag = app.screen.query_one("#dag-panel")
        history = app.screen.query_one("#run-history-content", RunHistoryWidget)
        sidebar = app.screen.query_one("#sidebar", Sidebar)
        detail = RunDetailWidget()
        detail._test_store = app.store
        assert "cancelled" in dag.render().plain
        assert "cancelled" in detail.render().plain
        assert "parked" in history.render().plain
        assert "parked" in sidebar.render().plain
        assert "parked" in app.screen.query_one("#status-bar").render().plain
        assert app.store.run_state_label == "parked"
        assert "◇" in dag.render().plain
        assert "◇" in detail.render().plain
        assert "◇" in history.render().plain
        assert "◇" in sidebar.render().plain
        assert app.store.node_elapsed[ids[0]] == 2.0
        assert not app.can_cancel_selected_run()
        await pilot.press("i")
        assert json.loads(app.screen.content)["state"] == "cancelled"
        await pilot.press("escape")
        # Every provider label stays verbatim, even when it resembles a built-in.
        for label in (
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "parked",
            "indeterminate",
            "mystery_state",
        ):
            current = app.store.current_run
            assert current is not None
            if label == "failed":
                # Equal labels must not reuse an enum's cached failure glyph.
                current.nodes[ids[0]].status = NodeStatus.FAILED
                assert "✗" in dag.render().plain
            current.status = status(label)
            current.nodes[ids[0]].status = status(label)
            for cached in app.store.runs_for_current_workflow:
                cached.status = current.status
            app.store.workflow_statuses[workflow.selector] = current.status
            app._refresh_widgets()
            await pilot.pause()
            for rendered in (
                dag.render(),
                detail.render(),
                history.render(),
                sidebar.render(),
            ):
                assert label in rendered.plain
                assert "◇" in rendered.plain
                assert "✓" not in rendered.plain
                assert "✗" not in rendered.plain
            assert app.store.run_state_label == label
            assert app.store.current_run.status is current.status


@pytest.mark.asyncio
async def test_revamp_node_attempt_detail_and_context_help_are_readable():
    from tui.screens.tools import InspectionScreen

    app = AvalancheApp(workflow="agent_trace", node="inspect_agent")
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        await pilot.press("i")
        assert isinstance(app.screen, InspectionScreen)
        assert json.loads(app.screen.content)["node"] == "inspect_agent_1"
        await pilot.press("escape", "?")
        assert isinstance(app.screen, InspectionScreen)
        assert app.store.selected_run_id in app.screen.content
        await pilot.press("escape", "enter")
        assert app.store.trace_inspector_open
        assert app.screen.query_one("#agent-trace-content").render().plain


@pytest.mark.asyncio
async def test_revamp_streaming_log_window_follow_pause_search_and_all_filters():
    from tui.models import LogWindow
    from tui.screens.tools import LogFilterScreen

    provider = _RevampProvider()
    run = provider.list_runs("order_workflow")[0]
    run.logs = [
        LogEntry(
            datetime(2020, 1, 1),
            LogLevel.INFO,
            "fetch_orders_1",
            f"message-{i}",
            channel="stdout",
            attempt="attempt-1",
        )
        for i in range(10020)
    ]
    run.latest_log_sequence = 10020
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.store.current_run is not None)
        widget = app.screen.query_one("#log-content", LogWidget)
        assert isinstance(app.store.logs, LogWindow)
        assert len(app.store.logs) == 10000
        assert "evicted" in widget._marker
        await pilot.press("s")
        assert not app._log_autoscroll
        detail = LogDetailAppended(
            run.operator_instance_id,
            run.run_id,
            run.created_sequence,
            10021,
            10021,
            LogEntry(
                datetime(2020, 1, 1),
                LogLevel.ERROR,
                "fetch_orders_1",
                "needle",
                channel="stderr",
                attempt="attempt-2",
            ),
        )
        for callback in provider._detail_callbacks:
            callback(detail)
        await _wait_for(pilot, lambda: app.store.logs[-1].message == "needle")
        await pilot.press("/", *"needle", "enter")
        assert app.store.match_count == 1
        await pilot.press("escape", "f")
        assert isinstance(app.screen, LogFilterScreen)
        app.screen.query_one("#filter-channel").value = "stderr"
        app.screen.query_one("#filter-attempt").value = "attempt-2"
        await pilot.click("#filter-apply")
        assert len(widget._rows) == 1
        assert widget._rows[0][1].message == "needle"
        assert app.store.logs.retained_bytes <= 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_revamp_schema_start_form_preserves_typed_kwargs_and_confirms():
    from tui.screens.input_form import InputForm

    provider = _RevampProvider()
    app = AvalancheApp(provider=provider, workflow="order_workflow")
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(
            pilot,
            lambda: app.extension_session.description is not None
            and app.extension_session.context == app.extension_context,
        )
        await pilot.press("r")
        assert isinstance(app.screen, InputForm)
        app.screen.query_one("#field-0").value = "0"
        await pilot.click("#form-validate")
        assert app.screen.query_one("#form-submit").disabled
        app.screen.query_one("#field-0").value = "7"
        # Textual ignores repeat clicks during the button's active animation;
        # wait for that actual UI state rather than depending on machine speed.
        await _wait_for(
            pilot, lambda: not app.screen.query_one("#form-validate").has_class("-active")
        )
        await pilot.click("#form-validate")
        assert not app.screen.query_one("#form-submit").disabled
        assert provider.starts == []
        await pilot.click("#form-submit")
        await _wait_for(pilot, lambda: bool(provider.starts))
        assert provider.starts == [("order_workflow", {"count": 7, "options": None})]
        assert provider.tui_extensions.calls[-1][0] == "invoke"
    assert provider.tui_extensions.unsubscribed


@pytest.mark.asyncio
async def test_schedules_panel_enables_and_disables_the_selected_workflow():
    from textual.widgets import DataTable

    from tui.screens.input_form import InputForm
    from tui.screens.tools import ResourceScreen

    app = AvalancheApp(workflow="ingest_workflow")
    provider = app.store.provider
    selector = INGEST_WORKFLOW.selector

    def advertised() -> set[str]:
        description = app.extension_session.description
        return {action.id for action in description.actions} if description else set()

    def enabled_cell() -> str:
        table = app.screen.query_one("#resource-body", DataTable)
        columns = [column.label.plain for column in table.columns.values()]
        row = next(
            table.get_row_at(index)
            for index in range(table.row_count)
            if table.get_row_at(index)[0] == json.dumps(selector)
        )
        return row[columns.index("enabled")]

    async def confirm() -> None:
        await _wait_for(pilot, lambda: isinstance(app.screen, InputForm))
        await pilot.click("#form-validate")
        await pilot.click("#form-submit")
        await _wait_for(pilot, lambda: isinstance(app.screen, ResourceScreen))

    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: "schedule.enable" in advertised())
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, ResourceScreen)
        await _wait_for(pilot, lambda: enabled_cell() == "false")
        await _wait_for(pilot, lambda: app.screen.query_one("#panel-action-0").display)

        await pilot.press("1")
        await confirm()
        await _wait_for(pilot, lambda: enabled_cell() == "true")
        assert provider._workflows[selector].schedule_enabled is True
        assert "schedule.disable" in advertised()
        # Panel buttons follow the advertised actions on their refresh interval.
        await _wait_for(pilot, lambda: app.screen.query_one("#panel-action-1").display)
        assert not app.screen.query_one("#panel-action-0").display

        await pilot.press("1")
        await confirm()
        await _wait_for(pilot, lambda: enabled_cell() == "false")
        assert provider._workflows[selector].schedule_enabled is False
        await pilot.press("escape")
        assert app.screen is app._screen


@pytest.mark.asyncio
async def test_enabling_a_schedule_asks_for_input_prefilled_with_defaults():
    from textual.widgets import Input

    from tui.screens.input_form import InputForm

    app = AvalancheApp(workflow="ingest_workflow")
    provider = app.store.provider
    selector = INGEST_WORKFLOW.selector
    schema = {
        "type": "object",
        "properties": {"since_hours": {"type": "integer", "default": 1}},
    }
    provider._workflows[selector] = replace(
        provider._workflows[selector], input_schema_json=json.dumps(schema)
    )

    def advertised() -> set[str]:
        description = app.extension_session.description
        return {action.id for action in description.actions} if description else set()

    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: "schedule.enable" in advertised())
        await pilot.press("ctrl+s")
        await pilot.press("1")
        await _wait_for(pilot, lambda: isinstance(app.screen, InputForm))
        field = app.screen.query_one("#field-0", Input)
        assert field.value == "1"

        field.value = "6"
        await pilot.click("#form-validate")
        await pilot.click("#form-submit")
        await _wait_for(pilot, lambda: provider._workflows[selector].schedule_enabled)
        assert provider._workflows[selector].schedule_input == {"since_hours": 6}

        await _wait_for(pilot, lambda: "schedule.disable" in advertised())
        await pilot.press("1")
        await _wait_for(pilot, lambda: isinstance(app.screen, InputForm))
        assert not app.screen.query(Input)


@pytest.mark.asyncio
async def test_run_history_header_marks_a_disabled_schedule():
    app = AvalancheApp(workflow="ingest_workflow")
    async with app.run_test(size=(160, 50)) as pilot:
        history = app._screen.query_one("#run-history")
        await _wait_for(pilot, lambda: "disabled" in str(history.border_title))
        assert "next run" not in str(history.border_title)
        app.store.provider.set_schedule_enabled(INGEST_WORKFLOW.selector, True)
        await _wait_for(pilot, lambda: "next run" in str(history.border_title))
        assert "disabled" not in str(history.border_title)


@pytest.mark.asyncio
async def test_schedules_panel_offers_no_controls_for_an_unscheduled_workflow():
    from tui.screens.tools import ResourceScreen

    app = AvalancheApp(workflow="order_workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(pilot, lambda: app.extension_session.description is not None)
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, ResourceScreen)
        await pilot.pause()
        assert not any(
            button.display for button in app.screen.query("#resource-actions Button")
        )
        assert not any(
            action.id.startswith("schedule.")
            for action in app.extension_session.description.actions
        )


@pytest.mark.asyncio
async def test_revamp_palette_and_read_only_block_keyboard_and_direct_mutations():
    provider = _RevampProvider()
    app = AvalancheApp(provider=provider, read_only=True)
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: app.extension_session.description is not None)
        await pilot.press("r", "x", "ctrl+p")
        assert app.screen is not app._screen
        await pilot.press("escape")
        action = app.extension_session.description.actions[0]
        app.invoke_extension_action(action)
        app.action_start_run()
        app.action_cancel_run()
        from tui.state import TuiExtensionError

        with pytest.raises(TuiExtensionError) as rejected:
            app.store.provider.start_run("order_workflow")
        assert rejected.value.code == "read_only"
        with pytest.raises(TuiExtensionError):
            app.store.provider.cancel_run("run-1")
        with pytest.raises(TuiExtensionError):
            app.store.provider.tui_extensions.invoke(
                action.id, app.extension_context, {}, intent_id="read-only-intent"
            )
        assert not app.can_start_run()
        assert provider.starts == []
        assert all(call[0] != "invoke" for call in provider.tui_extensions.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("renderer", ["table", "tree", "json", "diff"])
async def test_revamp_resource_panels_page_empty_error_and_reject_stale_context(renderer):
    from tui.screens.tools import ResourceScreen

    provider = _RevampProvider()
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(
            pilot,
            lambda: app.extension_session.description is not None
            and app.extension_session.context == app.extension_context,
        )
        panel = next(
            p for p in app.extension_session.description.panels if p.renderer == renderer
        )
        app.open_extension_panel(panel)
        assert isinstance(app.screen, ResourceScreen)
        await _wait_for(pilot, lambda: app.screen._next_cursor is not None)
        await pilot.click("#resource-next")
        await _wait_for(pilot, lambda: app.screen._next_cursor is None)
        provider.tui_extensions.fail_reads = True
        await pilot.click("#resource-reload")
        await _wait_for(
            pilot,
            lambda: "unavailable" in app.screen.query_one("#resource-status").render().plain,
        )
        app.store.switch_workflow(app.store.workflows[-1])
        await pilot.click("#resource-reload")
        assert "Stale" in app.screen.query_one("#resource-status").render().plain


@pytest.mark.asyncio
async def test_revamp_extension_completion_cannot_repaint_after_context_switch():
    provider = _RevampProvider()
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(
            pilot,
            lambda: app.extension_session.description is not None
            and app.extension_session.context == app.extension_context,
        )
        entered, release = threading.Event(), threading.Event()
        provider.tui_extensions.gate = (entered, release)
        panel = app.extension_session.description.panels[0]
        app.open_extension_panel(panel)
        try:
            await _wait_for(pilot, entered.is_set)
            screen = app.screen
            app.store.switch_workflow(app.store.workflows[-1])
            await pilot.pause(0.1)
            release.set()
            await pilot.pause(0.1)
            assert screen._revision is None
            assert screen.context != app.extension_context
        finally:
            release.set()


@pytest.mark.asyncio
async def test_revamp_run_deep_link_resolves_once_and_rejects_wrong_flow():
    provider = _RevampProvider()
    run = provider.list_runs("order_workflow")[0]
    app = AvalancheApp(
        provider=provider,
        workflow="order_workflow",
        run=run.run_id,
        node="validate_1",
        read_only=True,
    )
    async with app.run_test(size=(160, 50)) as pilot:
        await _wait_for(
            pilot,
            lambda: app.store.selected_run_id == run.run_id
            and app.store.selected_node is not None,
        )
        assert app.store.selected_node.name == "validate_1"
        assert provider.reads.count("run") == 1
    wrong = AvalancheApp(provider=provider, workflow="ingest_workflow", run=run.run_id)
    async with wrong.run_test(size=(160, 50)) as pilot:
        await _wait_for(pilot, lambda: bool(wrong.store.run_error))
        assert wrong.store.selected_run_id != run.run_id


def test_claude_declarations_fill_the_metadata_sections(store):
    from avalanche.agent.declaration import (
        AgentDeclaration,
        ClaudeAgentDeclaration,
        SignatureDeclaration,
        SkillDeclaration,
        ToolDeclaration,
    )

    declaration = AgentDeclaration(
        signature=SignatureDeclaration(name="Sum", instructions="Add.", inputs=(), outputs=()),
        tools=(ToolDeclaration(name="lookup", description="", source_code=""),),
        skills=(
            SkillDeclaration(
                name="house-style",
                description="Applies the house style.",
                instructions="Be brief.",
                files=("references/style.md",),
            ),
        ),
        runtime=ClaudeAgentDeclaration(
            model="claude-haiku-5-5", settings={"max_turns": 4}, builtin_tools=("Read",)
        ),
    )
    store.current_workflow = replace(
        store.current_workflow, agent_declarations={"agent": declaration}
    )
    store.select_node(store.all_nodes[0])

    sections = store.agent_declaration_sections()

    assert sections["models"] == {"main": "claude-haiku-5-5"}
    assert sections["runtime"] == {"max_turns": 4, "builtin_tools": ["Read"]}
    assert [tool["name"] for tool in sections["tools"]] == ["lookup"]
    assert sections["skills"] == {
        "house-style": {
            "description": "Applies the house style.",
            "instructions": "Be brief.",
            "files": ["references/style.md"],
        }
    }


def test_openai_declarations_fill_the_metadata_sections(store):
    from avalanche.agent.declaration import (
        AgentDeclaration,
        OpenAIAgentDeclaration,
        SignatureDeclaration,
        ToolDeclaration,
    )

    declaration = AgentDeclaration(
        signature=SignatureDeclaration(name="Sum", instructions="Add.", inputs=(), outputs=()),
        tools=(ToolDeclaration(name="lookup", description="", source_code=""),),
        skills=(),
        runtime=OpenAIAgentDeclaration(
            model="gpt-5.4-mini",
            settings={"max_turns": 4, "narrate": True},
            builtin_tools=("shell",),
        ),
    )
    store.current_workflow = replace(
        store.current_workflow, agent_declarations={"agent": declaration}
    )
    store.select_node(store.all_nodes[0])

    sections = store.agent_declaration_sections()

    assert sections["models"] == {"main": "gpt-5.4-mini"}
    assert sections["runtime"] == {"max_turns": 4, "narrate": True, "builtin_tools": ["shell"]}
    assert [tool["name"] for tool in sections["tools"]] == ["lookup"]
    assert sections["skills"] == {}
