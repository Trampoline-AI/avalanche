"""Logs, agent and classifier events, traces and evaluations survive operator restarts."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

import pytest

from runtime.operator.client import GrpcStateProvider, _DetailHydrationRaceError
from runtime.operator.evaluation_models import EvaluationRecord
from runtime.operator.models import (
    AgentEvent,
    ClassifierChoiceSummary,
    ClassifierEvent,
    ClassifierScoreSummary,
    LogEntry,
    LogLevel,
    NodeState,
    RunState,
    RunStatus,
    SequencedLogEntry,
    TraceDescriptor,
    TraceHeader,
    WorkflowTopology,
)
from runtime.operator.operator import OPERATOR_RESTARTED_REASON, Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.state import OperatorStateStore, StateRowError, StateStoreError
from runtime.operator.state.agent_events import RunAgentEventsTable
from runtime.operator.state.classifier_events import RunClassifierEventsTable
from runtime.operator.state.evaluations import RunEvaluationsTable
from runtime.operator.state.logs import RunLogsTable
from runtime.operator.state.run_details import delete_run_details, load_run_detail_totals
from runtime.operator.state.runs import RunRequest, RunsTable, StoredRun
from runtime.operator.state.traces import RunTracesTable, _StoredDescriptor, _StoredHeader

from .test_classifier_transport import _event_handle, _invocation, _publish, _seed_run
from .test_state_detail import _add_run, _finished, _live, _wire
from .test_state_detail import _event_handle as _state_event_handle

_TERMINAL = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}

_PRINTING_FLOW = """import time

import avalanche as ava


@ava.source
def talk_then_wait() -> int:
    print("before the crash", flush=True)
    time.sleep(300)
    return 1


@ava.workflow
def talker():
    return talk_then_wait()
"""

# Runs an operator in its own process, starts the talker workflow and reports the
# run once its log line arrived, then waits to be killed.
_KILLED_OPERATOR = """
import json, sys, time
from runtime.operator.operator import Operator

operator = Operator(
    [sys.argv[1]], watch=False, schedule=False, state_db=sys.argv[2], prepare_timeout=120
)
run_id = operator.start_run("talker")
deadline = time.monotonic() + 120
while not any("before the crash" in entry.message for entry in operator.get_run(run_id).logs):
    assert time.monotonic() < deadline, "talker run did not log"
    time.sleep(0.05)
print(json.dumps({"run_id": run_id}), flush=True)
sys.stdin.read()
"""


def _operator(paths: list[Path], state_db: Path | str) -> Operator:
    return Operator(
        [str(path) for path in paths],
        watch=False,
        schedule=False,
        webhook_port=0,
        state_db=state_db,
        prepare_timeout=120,
    )


def _wait_terminal(operator: Operator, run_id: str) -> RunState:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in _TERMINAL:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish")


def _logs(operator: Operator, run_id: str, **options) -> list[tuple]:
    """Every log descriptor and body of a run, paged one entry at a time."""
    page = operator.list_logs(run_id, page_size=1, **options)
    items = []
    while True:
        items.extend(
            (
                log.sequence,
                log.timestamp,
                log.level,
                log.node_id,
                log.size_bytes,
                log.channel,
                log.attempt,
                operator.read_detail(log.body_token),
            )
            for log in page.logs
        )
        if not page.next_page_token:
            return items
        page = operator.list_logs(page_token=page.next_page_token, page_size=1, **options)


def _agent_events(operator: Operator, run_id: str, node_id: str, **options) -> list[tuple]:
    page = operator.list_agent_events(run_id, node_id, page_size=1, **options)
    items = []
    while True:
        items.extend(
            (replace(event, body_token=""), operator.read_detail(event.body_token))
            for event in page.events
        )
        if not page.next_page_token:
            return items
        page = operator.list_agent_events(
            page_token=page.next_page_token, page_size=1, **options
        )


def _agent_view(operator: Operator, run_id: str) -> dict:
    """What a run view shows for a finished agent run, without per-instance tokens."""
    run = operator.get_run(run_id)
    assert run is not None
    snapshot = operator.get_latest_run_snapshot(
        run_id, operator_instance_id=operator.operator_instance_id
    )
    assert snapshot is not None
    agent_nodes = [node.node_id for node in run.nodes.values() if node.trace is not None]
    assert agent_nodes
    return {
        "logs": _logs(operator, run_id),
        "logs_newest_first": _logs(operator, run_id, order=1),
        "latest_log_sequence": snapshot.latest_log_sequence,
        "log_entries": run.logs,
        "agents": {
            node_id: {
                "events": _agent_events(operator, run_id, node_id),
                "events_newest_first": _agent_events(operator, run_id, node_id, order=1),
                "trace": run.nodes[node_id].trace,
                "trace_body": operator.read_trace(
                    run_id, node_id, operator_instance_id=operator.operator_instance_id
                ),
                "trace_json": run.nodes[node_id].agent_trace_json,
            }
            for node_id in agent_nodes
        },
    }


def test_an_agent_run_shows_the_same_details_after_a_restart(tmp_path):
    state_db = tmp_path / "state.db"
    operator = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        run = _add_run(operator, "agent-run")
        for evidence in (_live(1), _live(2, code="print(1)"), _finished()):
            operator._apply_event(run.run_id, _state_event_handle(), _wire(evidence))
        finished_revision = operator.get_run(run.run_id).nodes["agent_1"].trace.revision
        # A later invocation on the same step keeps serving the finished trace.
        operator._apply_event(
            run.run_id, _state_event_handle(), _wire(_live(1, invocation_id="second"))
        )
        operator._apply_event(
            run.run_id,
            _state_event_handle(),
            {
                "type": "log",
                "node_id": "agent_1",
                "level": 30,
                "message": "héllo",
                "timestamp": 1_700_000_000.25,
                "channel": "stderr",
                "attempt": "1",
            },
        )
        before = _agent_view(operator, run.run_id)
    finally:
        operator.close()
    [agent] = before["agents"].values()
    assert len(agent["events"]) == 3
    assert agent["trace"].available and agent["trace_body"].data
    assert agent["trace_body"].revision == agent["trace"].revision
    assert len(before["logs"]) == 5

    restarted = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        assert _agent_view(restarted, run.run_id) == before
        # Both published trace revisions still read the one stored body.
        assert finished_revision < agent["trace"].revision
        for revision in (finished_revision, agent["trace"].revision):
            assert restarted.read_trace(
                run.run_id,
                "agent_1",
                operator_instance_id=restarted.operator_instance_id,
                revision=revision,
            ) == replace(agent["trace_body"], revision=revision)
    finally:
        restarted.close()


def test_an_agent_trace_left_recording_ends_with_its_interrupted_run(tmp_path):
    state_db = tmp_path / "state.db"
    operator = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        run = _add_run(operator, "interrupted")
        operator._apply_event(run.run_id, _state_event_handle(), _wire(_live(1)))
        recording = operator.get_run(run.run_id).nodes["agent_1"].trace
        assert recording is not None and recording.status == "in_progress"
    finally:
        operator.close()

    restarted = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        failed = restarted.get_run(run.run_id)
        assert failed is not None and failed.status is RunStatus.FAILED
        trace = failed.nodes["agent_1"].trace
        assert trace is not None and trace.status == "error"
        assert trace.revision > recording.revision
        envelope = json.loads(failed.nodes["agent_1"].agent_trace_json)
        assert envelope["error"] == OPERATOR_RESTARTED_REASON
        assert len(_agent_events(restarted, run.run_id, "agent_1")) == 1
    finally:
        restarted.close()

    # Stored, so it reads the same after another restart.
    again = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        assert again.get_run(run.run_id).nodes["agent_1"].trace == trace
    finally:
        again.close()


def test_details_cannot_be_read_from_a_closed_operator(tmp_path):
    operator = Operator([], watch=False, schedule=False, webhook_port=0, state_db=":memory:")
    run = _add_run(operator, "closed")
    operator._apply_event(run.run_id, _state_event_handle(), _wire(_live(1)))
    operator.close()
    with pytest.raises(StateStoreError, match="closed"):
        operator.list_logs(run.run_id)
    with pytest.raises(StateStoreError, match="closed"):
        operator.list_agent_events(run.run_id, "agent_1")


def test_stored_trace_descriptors_name_every_domain_field():
    assert {field.name for field in fields(TraceDescriptor)} == set(
        _StoredDescriptor.model_fields
    )
    assert {field.name for field in fields(TraceHeader)} == set(_StoredHeader.model_fields)


def test_logs_received_before_a_kill_survive_it(tmp_path):
    flows = tmp_path / "flows.py"
    flows.write_text(_PRINTING_FLOW)
    state_db = tmp_path / "state.db"
    killed = subprocess.Popen(
        [sys.executable, "-c", _KILLED_OPERATOR, str(flows), str(state_db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert killed.stdout is not None
        run_id = json.loads(killed.stdout.readline())["run_id"]
    finally:
        killed.kill()
        killed.wait(timeout=30)

    restarted = _operator([flows], state_db)
    try:
        run = restarted.get_run(run_id)
        assert run is not None and run.status is RunStatus.FAILED
        assert any("before the crash" in entry.message for entry in run.logs)
        page = restarted.list_logs(run_id)
        assert [log.sequence for log in page.logs] == list(range(1, len(run.logs) + 1))
    finally:
        restarted.close()


def test_classifier_snapshots_survive_a_restart_with_their_bodies(tmp_path):
    state_db = tmp_path / "state.db"
    operator = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    records = [_invocation(0, "running"), _invocation(0, "success")]
    try:
        run = _seed_run(operator, "classified")
        for record in records:
            _publish(operator, run.run_id, record)
        operator._apply_event(
            run.run_id,
            _event_handle(operator),
            {"type": "terminal", "status": "failed", "error": "Postprocessing failed"},
        )
        before = operator.list_classifier_events(run.run_id, "classify").events
    finally:
        operator.close()

    restarted = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        after = restarted.list_classifier_events(run.run_id, "classify").events
        assert [replace(item, body_token="") for item in after] == [
            replace(item, body_token="") for item in before
        ]
        assert [restarted.read_detail(item.body_token) for item in after] == [
            record.model_dump_json().encode() for record in records
        ]
        newest_first = restarted.list_classifier_events(run.run_id, "classify", order=1)
        assert [item.event_sequence for item in newest_first.events] == [2, 1]
    finally:
        restarted.close()


def _evaluated_run(operator: Operator, run_id: str) -> RunState:
    run = RunState(
        run_id=run_id,
        flow_name="flow",
        workflow_id="flow.py::flow",
        workflow_display_name="flow",
        status=RunStatus.SUCCESS,
        started_at=100.0,
        ended_at=101.0,
        nodes={"judged": NodeState("judged", "judged", "step")},
        topology=WorkflowTopology(node_ids=("judged",)),
    )
    with operator._lock:
        operator._runs[run_id] = run
    operator._notify_run(run)
    return run


def test_a_pending_evaluation_is_failed_after_a_restart_and_finished_ones_are_kept(tmp_path):
    state_db = tmp_path / "state.db"
    failed = EvaluationRecord(
        evaluation_id="evaluation-1",
        run_id="judged-run",
        node_id="judged",
        status="failed",
        created_at=100.0,
        ended_at=101.0,
        error="Evaluation service refused the request",
    )
    pending = EvaluationRecord(
        evaluation_id="evaluation-2",
        run_id="judged-run",
        node_id="judged",
        status="pending",
        created_at=102.0,
    )
    operator = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        _evaluated_run(operator, "judged-run")
        with operator._state_store.transaction() as connection:
            RunEvaluationsTable.insert(connection, failed)
            RunEvaluationsTable.insert(connection, pending)
        assert operator.list_evaluations("judged-run") == [failed, pending]
    finally:
        operator.close()

    started = time.time()
    restarted = Operator([], watch=False, schedule=False, webhook_port=0, state_db=state_db)
    try:
        kept, restarted_record = restarted.list_evaluations("judged-run")
        assert kept == failed
        assert restarted_record.status == "failed"
        assert restarted_record.error == OPERATOR_RESTARTED_REASON
        assert restarted_record.created_at == pending.created_at
        assert restarted_record.ended_at is not None and restarted_record.ended_at >= started
        assert restarted.list_evaluations("judged-run", "judged") == [kept, restarted_record]
    finally:
        restarted.close()


_EVENT_ULID = "0" * 26


def _trace_reference(scope: str, sha256: str) -> pb.ActivityDetailRefV2:
    return pb.ActivityDetailRefV2(
        run_id="run-1",
        scope_ref=pb.ScopeReferenceV2(reference=scope),
        activity_id="trace:agent:7",
        run_sequence=3,
        object_uri="avalanche-trace://run-1/agent/7",
        object_key="run-1/agent/7",
        sha256=sha256,
        size_bytes=10,
    )


def test_a_client_takes_a_restarted_operators_references_for_the_same_details():
    provider = GrpcStateProvider("127.0.0.1:1")
    try:
        provider._install_structural_baseline("operator-1", _EVENT_ULID, {})
        provider._remember_detail_reference(_trace_reference("operator-1", "a" * 64))
        # Before its stream reconnects, the client already reads the restarted
        # operator, which serves the stored trace under the same key.
        provider._remember_detail_reference(_trace_reference("operator-2", "a" * 64))
        # Once the client installs the restarted operator's baseline, a late reply
        # from the previous one is never bound again.
        provider._install_structural_baseline("operator-2", _EVENT_ULID, {})
        with pytest.raises(_DetailHydrationRaceError):
            provider._remember_detail_reference(_trace_reference("operator-1", "a" * 64))
        # Within one operator a reference never changes.
        provider._remember_detail_reference(_trace_reference("operator-2", "a" * 64))
        with pytest.raises(_DetailHydrationRaceError):
            provider._remember_detail_reference(_trace_reference("operator-2", "b" * 64))
    finally:
        provider.close()


# ── Adapters ─────────────────────────────────────────────


def _store_run(connection, run_id: str) -> None:
    RunsTable.create(
        connection,
        StoredRun(
            run=RunState(run_id=run_id, flow_name="flow", workflow_id="flow.py::flow"),
            created_sequence=1,
            revision=1,
            activity_sequence=0,
            node_revisions={},
        ),
        RunRequest(),
        None,
    )


def _all_details(connection, run_id: str) -> None:
    RunLogsTable.insert(
        connection,
        run_id,
        SequencedLogEntry(
            sequence=1,
            entry=LogEntry(
                timestamp=datetime(2026, 11, 3, 9, 30, 0, 123456),
                level=LogLevel.WARN,
                node_id="agent",
                message="héllo",
                channel="stderr",
                attempt="1",
            ),
            size_bytes=6,
        ),
    )
    RunAgentEventsTable.insert(
        connection,
        run_id,
        "agent",
        AgentEvent(
            invocation_id="call-1",
            event_sequence=1,
            event_json='{"kind": "turn"}',
            size_bytes=16,
            agent_event_kind="turn.finished",
            turn=1,
            duration_ms=5,
            error=True,
            tool_call_count=2,
        ),
    )
    RunClassifierEventsTable.insert(
        connection,
        run_id,
        "classify",
        ClassifierEvent(
            invocation_id="call-1",
            invocation_index=0,
            answers=(
                ClassifierChoiceSummary("route", "accept"),
                ClassifierScoreSummary("q", 1),
            ),
            event_sequence=1,
            event_json="{}",
            size_bytes=2,
            event_kind="success",
        ),
    )
    RunTracesTable.insert_body(connection, run_id, "agent", 5, b"trace body")
    RunTracesTable.carry_body(connection, run_id, "agent", 6, 5)
    RunTracesTable.write_descriptor(
        connection, run_id, "agent", TraceDescriptor(status="succeeded", revision=6), "boom"
    )
    RunEvaluationsTable.insert(
        connection,
        EvaluationRecord(
            evaluation_id="evaluation-1",
            run_id=run_id,
            node_id="agent",
            status="pending",
            created_at=1.0,
        ),
    )


def test_detail_tables_round_trip_and_delete_one_run_only():
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            for run_id in ("kept", "deleted"):
                _store_run(connection, run_id)
                _all_details(connection, run_id)
            [log] = RunLogsTable.list_run(connection, "kept")
            assert log.entry.timestamp == datetime(2026, 11, 3, 9, 30, 0, 123456)
            assert log.entry.level is LogLevel.WARN and log.entry.message == "héllo"
            [event] = RunAgentEventsTable.list_node(connection, "kept", "agent")
            assert event.error is True and event.agent_event_kind == "turn.finished"
            [snapshot] = RunClassifierEventsTable.page(
                connection, "kept", "classify", after=0, through=1, limit=5
            )
            assert snapshot.answers == (
                ClassifierChoiceSummary("route", "accept"),
                ClassifierScoreSummary("q", 1.0),
            )
            latest = RunTracesTable.read_body(connection, "kept", "agent", None)
            assert latest is not None and (latest.revision, latest.data) == (6, b"trace body")
            assert RunTracesTable.read_body(connection, "kept", "agent", 4) is None
            totals = load_run_detail_totals(connection)
            assert totals.last_log_sequences["kept"] == 1
            # One body, stored once though served at two revisions.
            assert totals.node_detail_bytes[("kept", "agent")] == 16 + len(b"trace body")
            assert totals.run_detail_bytes["kept"] == 6 + 16 + 2 + len(b"trace body")

            deleted = delete_run_details(connection, "deleted")
            assert (
                deleted.logs,
                deleted.agent_events,
                deleted.classifier_events,
                deleted.trace_bodies,
                deleted.evaluations,
            ) == (1, 1, 1, 1, 1)
            assert RunLogsTable.list_run(connection, "deleted") == ()
            assert RunTracesTable.read_body(connection, "deleted", "agent", None) is None
            assert RunEvaluationsTable.list_run(connection, "deleted") == ()
            assert len(RunLogsTable.list_run(connection, "kept")) == 1
            assert [trace.run_id for trace in RunTracesTable.load_descriptors(connection)] == [
                "kept"
            ]
            # Deleting the run itself removes whatever details are left.
            RunsTable.delete(connection, "kept")
            assert load_run_detail_totals(connection).traces == ()
            assert RunEvaluationsTable.list_pending(connection) == ()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("run_logs", "timestamp", "not a time"),
        ("run_agent_events", "agent_event_kind", "unknown"),
        ("run_classifier_events", "answers_json", '[{"question_id": "q"}]'),
        ("run_traces", "descriptor_json", '{"status": "succeeded"}'),
        ("run_evaluations", "record_json", '{"status": "pending"}'),
    ],
)
def test_a_stored_detail_that_does_not_match_its_model_is_an_error(table, column, value):
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            _store_run(connection, "run-1")
            _all_details(connection, "run-1")
            connection.execute(f"UPDATE {table} SET {column} = ?", (value,))
            with pytest.raises(StateRowError, match=table):
                RunLogsTable.list_run(connection, "run-1")
                RunAgentEventsTable.list_node(connection, "run-1", "agent")
                RunClassifierEventsTable.page(
                    connection, "run-1", "classify", after=0, through=1, limit=1
                )
                load_run_detail_totals(connection)
                RunEvaluationsTable.list_run(connection, "run-1")
    finally:
        store.close()
