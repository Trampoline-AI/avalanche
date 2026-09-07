"""Reducer idempotency, callback isolation, and slow-consumer recovery."""

import dataclasses
import json
from datetime import datetime

import pytest

from runtime.operator.client import GrpcStateProvider, _RunUpdateResetError
from runtime.operator.models import (
    AgentEvent,
    AgentEventDetailAppended,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    NodeSnapshot,
    NodeStatus,
    OperatorUpdate,
    OperatorUpdateEnvelope,
    RunCreated,
    RunState,
    RunStatus,
    RunStatusChanged,
    RunSummary,
    TerminalSealAppended,
    TerminalSealDescriptor,
)
from runtime.operator.operator import Operator


def _event_ulid(sequence: int) -> str:
    return f"{sequence:026X}"


def _operator_update(*, sequence: int, change, event_ulid: str | None = None) -> OperatorUpdate:
    return OperatorUpdate(
        sequence=sequence,
        event_ulid=event_ulid or _event_ulid(sequence),
        change=change,
    )


def _created(sequence: int = 1, *, epoch: str = "operator-1") -> OperatorUpdateEnvelope:
    return OperatorUpdateEnvelope(
        operator_instance_id=epoch,
        update=_operator_update(
            sequence=sequence,
            event_ulid=_event_ulid(sequence),
            change=RunCreated(
                summary=RunSummary(
                    run_id="run-1",
                    flow_name="flow",
                    status=RunStatus.PENDING,
                    workflow_id="flow.py::flow",
                    workflow_display_name="Flow",
                    created_sequence=sequence,
                    revision=sequence,
                ),
                nodes=(
                    NodeSnapshot(
                        node_id="node-1",
                        name="Node",
                        node_type="step",
                        revision=sequence,
                    ),
                ),
            ),
        ),
    )


def test_ordered_updates_ignore_replays_but_reject_conflicting_or_backwards_events():
    provider = GrpcStateProvider("localhost:1")
    try:
        pending, _ = provider._apply_update_envelope(_created())
        running = OperatorUpdateEnvelope(
            operator_instance_id="operator-1",
            update=_operator_update(
                sequence=2,
                change=RunStatusChanged("run-1", RunStatus.RUNNING, revision=2),
            ),
        )
        current, _ = provider._apply_update_envelope(running)
        assert current.status is RunStatus.RUNNING
        assert pending.status is RunStatus.PENDING
        assert provider._apply_update_envelope(running) == (None, None)
        conflict = dataclasses.replace(
            running,
            update=dataclasses.replace(
                running.update, change=RunStatusChanged("run-1", RunStatus.FAILED, revision=2)
            ),
        )
        with pytest.raises(_RunUpdateResetError):
            provider._apply_update_envelope(conflict)
        success = dataclasses.replace(
            running,
            update=_operator_update(
                sequence=4, change=RunStatusChanged("run-1", RunStatus.SUCCESS, revision=4)
            ),
        )
        current, _ = provider._apply_update_envelope(success)
        assert current.status is RunStatus.SUCCESS
        backwards = dataclasses.replace(
            running,
            update=_operator_update(
                sequence=3, change=RunStatusChanged("run-1", RunStatus.FAILED, revision=3)
            ),
        )
        with pytest.raises(_RunUpdateResetError):
            provider._apply_update_envelope(backwards)
        assert provider._runs_by_id["run-1"].status is RunStatus.SUCCESS
    finally:
        provider.close()


def test_client_applies_terminal_seal_independently_and_replays_idempotently():
    provider = GrpcStateProvider("localhost:1")
    seal = TerminalSealDescriptor(
        activity_id="terminal-seal-1",
        run_sequence=4,
        timestamp=datetime(2026, 7, 22),
        terminal_status=RunStatus.FAILED,
        reason="execution failed",
    )
    first = OperatorUpdateEnvelope(
        operator_instance_id="operator-1",
        update=_operator_update(
            sequence=2,
            change=TerminalSealAppended("run-1", seal),
        ),
    )
    try:
        provider._apply_update_envelope(_created())
        run, detail = provider._apply_update_envelope(first)
        assert run is not None
        assert detail is None
        assert run.status is RunStatus.PENDING
        assert run.terminal_seal == seal

        assert provider._apply_update_envelope(first) == (None, None)
        assert provider._cursor.event_ulid == _event_ulid(2)

        later_duplicate = dataclasses.replace(
            first,
            update=dataclasses.replace(
                first.update,
                sequence=3,
                event_ulid=_event_ulid(3),
            ),
        )
        assert provider._apply_update_envelope(later_duplicate) == (None, None)
        assert provider._cursor.event_ulid == _event_ulid(3)
        assert provider._runs_by_id["run-1"].terminal_seal == seal

        changed = dataclasses.replace(
            first,
            update=dataclasses.replace(
                first.update,
                sequence=4,
                event_ulid=_event_ulid(4),
                change=TerminalSealAppended(
                    "run-1",
                    dataclasses.replace(seal, reason="different failure"),
                ),
            ),
        )
        with pytest.raises(_RunUpdateResetError):
            provider._apply_update_envelope(changed)
        assert provider._cursor.event_ulid == _event_ulid(3)

        unknown_run = dataclasses.replace(
            first,
            update=dataclasses.replace(
                first.update,
                sequence=4,
                event_ulid=_event_ulid(4),
                change=TerminalSealAppended("missing-run", seal),
            ),
        )
        with pytest.raises(_RunUpdateResetError):
            provider._apply_update_envelope(unknown_run)
    finally:
        provider.close()


def test_mutating_callbacks_cannot_corrupt_other_consumers_or_retained_state():
    provider = GrpcStateProvider("localhost:1")
    try:
        run, _ = provider._apply_update_envelope(_created())
        assert run.details_hydrated is False

        observed_runs = []

        def mutate_run(projected):
            projected.status = RunStatus.FAILED
            projected.nodes["node-1"].status = NodeStatus.FAILED

        provider._run_callbacks.extend((mutate_run, observed_runs.append))
        provider._notify_run_callbacks(run)

        assert observed_runs[0].status is RunStatus.PENDING
        assert observed_runs[0].nodes["node-1"].status is NodeStatus.PENDING
        assert provider._runs_by_id["run-1"].status is RunStatus.PENDING
        assert provider._runs_by_id["run-1"].nodes["node-1"].status is NodeStatus.PENDING

        log = LogEntry(
            timestamp=datetime(2026, 7, 22),
            level=LogLevel.INFO,
            node_id="node-1",
            message="original",
        )
        provider._log_entries["run-1"] = [log]
        log_detail = LogDetailAppended(
            operator_instance_id="operator-1",
            run_id="run-1",
            created_sequence=1,
            sequence=2,
            log_sequence=1,
            log=log,
        )
        observed_details = []

        def mutate_detail(projected):
            if isinstance(projected, LogDetailAppended):
                projected.log.message = "corrupted"
            else:
                object.__setattr__(projected.event, "event_json", '{"corrupted":true}')

        provider._detail_callbacks.extend((mutate_detail, observed_details.append))
        provider._notify_detail_callbacks(log_detail)

        assert observed_details[0].log.message == "original"
        assert provider._log_entries["run-1"][0].message == "original"

        observed_logs = []

        def mutate_log(projected):
            projected.message = "corrupted"

        provider._log_callbacks.extend((mutate_log, observed_logs.append))
        provider._notify_log_callbacks(log)

        assert observed_logs[0].message == "original"
        assert provider._log_entries["run-1"][0].message == "original"

        event = AgentEvent(
            invocation_id="test-invocation",
            event_sequence=1,
            event_json='{"sequence":1,"event_kind":"original"}',
            size_bytes=46,
        )
        provider._agent_events[("run-1", "node-1")] = [event]
        event_detail = AgentEventDetailAppended(
            operator_instance_id="operator-1",
            run_id="run-1",
            created_sequence=1,
            sequence=3,
            node_id="node-1",
            event=event,
        )
        provider._notify_detail_callbacks(event_detail)

        assert json.loads(observed_details[1].event.event_json)["event_kind"] == "original"
        assert (
            json.loads(provider._agent_events[("run-1", "node-1")][0].event_json)["event_kind"]
            == "original"
        )
    finally:
        provider.close()


def test_slow_consumer_gets_reset_instead_of_silently_losing_terminal_status():
    operator = Operator([], watch=False, schedule=False, subscriber_queue_capacity=2)
    run = RunState(run_id="run-1", flow_name="flow")
    operator._runs[run.run_id] = run
    try:
        operator._notify_run(run)
        subscription = operator.subscribe_operator_updates(
            operator.operator_instance_id, operator.current_sequence
        )
        for status in (RunStatus.RUNNING, RunStatus.SUCCESS):
            run.status = status
            operator._notify_run(run)
        reset = subscription.get(timeout=5)
        assert reset.reset_required is not None
        assert subscription.empty()
        snapshot = operator.get_latest_run_snapshot(
            run.run_id, operator_instance_id=operator.operator_instance_id
        )
        assert snapshot.summary.status is RunStatus.SUCCESS
        assert snapshot.terminal_seal.terminal_status is RunStatus.SUCCESS
    finally:
        operator.close()
