import dataclasses
import json
import logging
import threading
from datetime import datetime
from types import SimpleNamespace

import grpc
import pytest

import runtime.operator.client as client_module
from runtime.operator.client import (
    GrpcStateProvider,
    _DeltaResetError,
    _DetailHydrationRaceError,
)
from runtime.operator.convert import (
    run_delta_envelope_from_proto,
    run_delta_envelope_to_proto,
)
from runtime.operator.models import (
    AgentEvent,
    AgentEventAppended,
    AgentEventDescriptor,
    AgentEventDetailAppended,
    LogAppended,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    LogRecordDescriptor,
    NodeSnapshot,
    NodeState,
    NodeStatus,
    NodeStatusChanged,
    ResetBaseline,
    ResetRequired,
    RunCreated,
    RunDelta,
    RunDeltaEnvelope,
    RunSnapshot,
    RunState,
    RunStatus,
    RunStatusChanged,
    RunSummary,
    TraceDescriptor,
    TraceFinalized,
)
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb


def _event_handle() -> SimpleNamespace:
    return SimpleNamespace(
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
    )


def _drain(subscription):
    values = []
    while not subscription.empty():
        values.append(subscription.get_nowait())
    return values


def _created(sequence: int = 1, *, epoch: str = "operator-1") -> RunDeltaEnvelope:
    return RunDeltaEnvelope(
        operator_instance_id=epoch,
        delta=RunDelta(
            sequence=sequence,
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


def test_typed_delta_envelopes_roundtrip_all_changes():
    changes = [
        _created().delta.change,
        RunStatusChanged("run-1", RunStatus.RUNNING, started_at=1.0, revision=2),
        NodeStatusChanged("run-1", "node-1", NodeStatus.SUCCESS, ended_at=2.0, revision=3),
        LogAppended(
            "run-1",
            LogRecordDescriptor(
                sequence=4,
                timestamp=datetime(2026, 7, 22),
                level=LogLevel.INFO,
                node_id="node-1",
                size_bytes=2,
                body_token="log-token",
            ),
        ),
        AgentEventAppended(
            "run-1",
            "node-1",
            AgentEventDescriptor(1, 14, "event-token"),
        ),
        TraceFinalized(
            "run-1",
            "node-1",
            TraceDescriptor(
                status="completed",
                revision=6,
                available=True,
                complete=True,
                event_count=1,
                size_bytes=20,
            ),
        ),
    ]

    for sequence, change in enumerate(changes, start=1):
        envelope = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(sequence=sequence, change=change),
        )
        assert run_delta_envelope_from_proto(run_delta_envelope_to_proto(envelope)) == envelope


def test_operator_replays_typed_deltas_in_order():
    operator = Operator([], watch=False, schedule=False, stream_history_capacity=16)
    run = RunState(run_id="run-1", flow_name="flow")
    run.nodes["node-1"] = NodeState("node-1", "Node", "step")
    operator._runs[run.run_id] = run
    try:
        operator._notify_run(run)
        operator._apply_event(
            run.run_id, _event_handle(), {"type": "running", "timestamp": 1.0}
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {"type": "node_started", "node_id": "node-1", "timestamp": 1.0},
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "log",
                "timestamp": 1.0,
                "level": logging.INFO,
                "node_id": "node-1",
                "message": "work",
            },
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "node-1",
                "event": {
                    "kind": "evidence",
                    "sequence": 1,
                    "event_kind": "code.executed",
                    "timestamp_ns": 1,
                    "data": {},
                },
            },
        )
        operator._apply_event(
            run.run_id,
            _event_handle(),
            {
                "type": "agent_evidence",
                "node_id": "node-1",
                "event": {
                    "kind": "trace_finished",
                    "trace": {
                        "status": "completed",
                        "evidence": {"run_id": "predict-run", "complete": True},
                        "steps": [],
                    },
                },
            },
        )

        envelopes = _drain(operator.subscribe_run_deltas(operator.operator_instance_id, 0))
        assert [envelope.delta.sequence for envelope in envelopes] == list(
            range(1, operator.current_sequence + 1)
        )
        assert [type(envelope.delta.change) for envelope in envelopes] == [
            RunCreated,
            RunStatusChanged,
            NodeStatusChanged,
            LogAppended,
            LogAppended,
            AgentEventAppended,
            TraceFinalized,
            LogAppended,
            TraceFinalized,
        ]
        replay = _drain(
            operator.subscribe_run_deltas(
                operator.operator_instance_id,
                envelopes[2].delta.sequence,
            )
        )
        assert replay == envelopes[3:]
    finally:
        operator.close()


def test_stale_cursor_and_epoch_explicitly_require_reset():
    operator = Operator([], watch=False, schedule=False, stream_history_capacity=2)
    run = RunState(run_id="run-1", flow_name="flow")
    operator._runs[run.run_id] = run
    try:
        operator._notify_run(run)
        for status in (RunStatus.RUNNING, RunStatus.SUCCESS):
            run.status = status
            operator._notify_run(run)

        stale_cursor = operator.subscribe_run_deltas(operator.operator_instance_id, 0)
        cursor_reset = stale_cursor.get_nowait()
        assert cursor_reset.delta is None
        assert cursor_reset.reset_required == ResetRequired(
            history_floor=2,
            latest_sequence=3,
        )

        stale_epoch = operator.subscribe_run_deltas("previous-operator", 3)
        epoch_reset = stale_epoch.get_nowait()
        assert epoch_reset.operator_instance_id == operator.operator_instance_id
        assert epoch_reset.reset_required is not None
        assert stale_epoch.empty()
    finally:
        operator.close()


def test_slow_delta_consumer_gets_bounded_overflow_reset_on_terminal_update():
    operator = Operator(
        [],
        watch=False,
        schedule=False,
        stream_history_capacity=16,
        subscriber_queue_capacity=2,
    )
    run = RunState(run_id="run-1", flow_name="flow")
    operator._runs[run.run_id] = run
    try:
        operator._notify_run(run)
        subscription = operator.subscribe_run_deltas(
            operator.operator_instance_id,
            operator.current_sequence,
        )
        for status in (
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.SUCCESS,
        ):
            run.status = status
            operator._notify_run(run)

        assert subscription.maxsize == 2
        assert subscription.qsize() == 1
        reset = subscription.get_nowait()
        assert reset.reset_required == ResetRequired(
            history_floor=1,
            latest_sequence=4,
        )
        assert subscription not in operator._delta_subscribers
        assert operator.get_run(run.run_id).status is RunStatus.SUCCESS
    finally:
        operator.close()


def test_legacy_full_state_subscription_surface_is_absent():
    operator = Operator([], watch=False, schedule=False)
    try:
        assert not hasattr(operator, "subscribe")
        assert not hasattr(operator, "unsubscribe")
        assert not hasattr(operator, "_subscribers")
    finally:
        operator.close()


def test_operator_restart_rejects_previous_epoch_cursor():
    first = Operator([], watch=False, schedule=False)
    second = Operator([], watch=False, schedule=False)
    try:
        run = RunState(run_id="run-1", flow_name="flow")
        first._runs[run.run_id] = run
        first._notify_run(run)

        subscription = second.subscribe_run_deltas(
            first.operator_instance_id,
            first.current_sequence,
        )
        envelope = subscription.get_nowait()
        assert envelope.operator_instance_id == second.operator_instance_id
        assert envelope.reset_required == ResetRequired(history_floor=1, latest_sequence=0)
    finally:
        first.close()
        second.close()


def test_bounded_journal_retains_deltas_not_run_state_snapshots():
    operator = Operator([], watch=False, schedule=False, stream_history_capacity=2)
    run = RunState(run_id="run-large", flow_name="flow")
    run.nodes = {
        f"node-{index}": NodeState(f"node-{index}", f"Node {index}", "step")
        for index in range(500)
    }
    operator._runs[run.run_id] = run
    try:
        operator._notify_run(run)
        for index, message in enumerate(("a" * 65_536, "tail"), start=1):
            operator._apply_event(
                run.run_id,
                _event_handle(),
                {
                    "type": "log",
                    "timestamp": float(index),
                    "level": logging.INFO,
                    "node_id": "node-1",
                    "message": message,
                },
            )

        assert len(operator._stream_history) == 2
        assert all(isinstance(item, RunDelta) for item in operator._stream_history)
        assert all(isinstance(item.change, LogAppended) for item in operator._stream_history)
        assert [item.change.log.size_bytes for item in operator._stream_history] == [
            65_536,
            4,
        ]
        assert all(not hasattr(item.change.log, "entry") for item in operator._stream_history)

        def contains_run_state(value):
            if isinstance(value, RunState):
                return True
            if dataclasses.is_dataclass(value):
                return any(
                    contains_run_state(getattr(value, field.name))
                    for field in dataclasses.fields(value)
                )
            if isinstance(value, (tuple, list)):
                return any(contains_run_state(item) for item in value)
            return False

        assert not any(contains_run_state(item) for item in operator._stream_history)
    finally:
        operator.close()


def test_client_applies_ordered_deltas_and_ignores_duplicates():
    provider = GrpcStateProvider("localhost:1")
    try:
        run, _ = provider._apply_delta_envelope(_created())
        assert run.status == RunStatus.PENDING

        status = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(
                sequence=2,
                change=RunStatusChanged("run-1", RunStatus.RUNNING, started_at=1.0, revision=2),
            ),
        )
        run, _ = provider._apply_delta_envelope(status)
        assert run.status == RunStatus.RUNNING
        assert provider._apply_delta_envelope(status) == (None, None)
        assert provider._cursor.sequence == 2

        node_delta = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(
                sequence=3,
                change=NodeStatusChanged(
                    "run-1",
                    "node-1",
                    NodeStatus.SUCCESS,
                    ended_at=2.0,
                    revision=3,
                ),
            ),
        )
        run, _ = provider._apply_delta_envelope(node_delta)
        assert run.nodes["node-1"].status == NodeStatus.SUCCESS

        provider._read_detail_body = lambda token, _size: {
            "log-token": b"complete",
            "event-token": b'{"sequence":1,"event_kind":"code.executed","data":{}}',
        }[token]
        log_delta = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(
                sequence=4,
                change=LogAppended(
                    "run-1",
                    LogRecordDescriptor(
                        sequence=1,
                        timestamp=datetime(2026, 7, 22),
                        level=LogLevel.INFO,
                        node_id="node-1",
                        size_bytes=8,
                        body_token="log-token",
                    ),
                ),
            ),
        )
        run, detail = provider._apply_delta_envelope(log_delta)
        assert isinstance(detail, LogDetailAppended)
        assert detail.log.message == "complete"
        assert run.logs == []
        assert run.latest_log_sequence == 1

        event_delta = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(
                sequence=5,
                change=AgentEventAppended(
                    "run-1",
                    "node-1",
                    AgentEventDescriptor(
                        event_sequence=1,
                        size_bytes=55,
                        body_token="event-token",
                    ),
                ),
            ),
        )
        run, _ = provider._apply_delta_envelope(event_delta)
        assert run.nodes["node-1"].agent_trace_json is None
        key = ("run-1", "node-1")
        assert provider._agent_events[key] == [
            {"sequence": 1, "event_kind": "code.executed", "data": {}}
        ]
        with provider._state_lock:
            materialized = provider._materialize_run_locked(run)
        assert json.loads(materialized.nodes["node-1"].agent_trace_json)["events"] == [
            {"sequence": 1, "event_kind": "code.executed", "data": {}}
        ]

        trace_delta = RunDeltaEnvelope(
            operator_instance_id="operator-1",
            delta=RunDelta(
                sequence=6,
                change=TraceFinalized(
                    "run-1",
                    "node-1",
                    TraceDescriptor(
                        status="completed",
                        revision=6,
                        available=True,
                        complete=True,
                        event_count=1,
                        size_bytes=20,
                    ),
                ),
            ),
        )
        run, _ = provider._apply_delta_envelope(trace_delta)
        assert run.nodes["node-1"].trace.status == "completed"
        assert run.nodes["node-1"].agent_trace_json is None
        with provider._state_lock:
            materialized = provider._materialize_run_locked(run)
        assert json.loads(materialized.nodes["node-1"].agent_trace_json)["status"] == (
            "completed"
        )
        provider._trace_bodies[key] = {"status": "completed", "marker": "revision-6"}
        provider._hydrated_trace_revisions[key] = 6

        run, _ = provider._apply_delta_envelope(
            RunDeltaEnvelope(
                operator_instance_id="operator-1",
                delta=RunDelta(
                    sequence=7,
                    change=TraceFinalized(
                        "run-1",
                        "node-1",
                        TraceDescriptor(
                            status="completed",
                            revision=7,
                            available=True,
                            complete=True,
                            event_count=1,
                            size_bytes=24,
                        ),
                    ),
                ),
            )
        )
        assert run.nodes["node-1"].trace.revision == 7
        assert run.nodes["node-1"].agent_trace_json is None
        assert key not in provider._trace_bodies
        assert key not in provider._hydrated_trace_revisions
        with provider._state_lock:
            materialized = provider._materialize_run_locked(run)
        assert json.loads(materialized.nodes["node-1"].agent_trace_json)["trace"] is None

        with pytest.raises(_DeltaResetError, match="sequence gap"):
            provider._apply_delta_envelope(
                RunDeltaEnvelope(
                    operator_instance_id="operator-1",
                    delta=RunDelta(
                        sequence=9,
                        change=RunStatusChanged("run-1", RunStatus.SUCCESS, revision=8),
                    ),
                )
            )
    finally:
        provider.close()


def test_client_log_delta_append_never_copies_cumulative_history():
    class AppendOnlyList(list):
        def __init__(self):
            super().__init__()
            self.append_count = 0

        def __iter__(self):
            raise AssertionError("live log append iterated cumulative history")

        def __getitem__(self, index):
            raise AssertionError("live log append indexed cumulative history")

        def append(self, item):
            self.append_count += 1
            return super().append(item)

    provider = GrpcStateProvider("localhost:1")
    try:
        initial, _ = provider._apply_delta_envelope(_created())
        structural_logs = AppendOnlyList()
        retained_logs = AppendOnlyList()
        initial.logs = structural_logs
        provider._log_entries["run-1"] = retained_logs

        for log_sequence in range(1, 1001):
            entry = LogEntry(
                timestamp=datetime(2026, 7, 22),
                level=LogLevel.INFO,
                node_id="node-1",
                message=str(log_sequence),
            )
            run, detail = provider._apply_delta_envelope_locked(
                RunDeltaEnvelope(
                    operator_instance_id="operator-1",
                    delta=RunDelta(
                        sequence=log_sequence + 1,
                        change=LogAppended(
                            "run-1",
                            LogRecordDescriptor(
                                sequence=log_sequence,
                                timestamp=entry.timestamp,
                                level=entry.level,
                                node_id=entry.node_id,
                                size_bytes=len(entry.message),
                                body_token=f"log-{log_sequence}",
                            ),
                        ),
                    ),
                ),
                log_detail=entry,
            )
            assert isinstance(detail, LogDetailAppended)
            assert run.logs is structural_logs

        assert retained_logs.append_count == 1000
        assert list.__len__(retained_logs) == 1000
        assert structural_logs.append_count == 0
        assert list.__len__(structural_logs) == 0
    finally:
        provider.close()


def test_client_delta_reducer_uses_copy_on_write_and_constant_event_append(
    monkeypatch,
):
    provider = GrpcStateProvider("localhost:1")
    try:
        initial, _ = provider._apply_delta_envelope(_created())
        key = ("run-1", "node-1")
        provider._hydrated_agent_nodes.add(key)
        provider._agent_event_sequences[key] = 0
        provider._agent_events[key] = []
        event_container = provider._agent_events[key]
        original_loads = client_module.json.loads
        parsed_event_bodies = []

        def reject_full_copy(_value):
            raise AssertionError("delta reducer deep-copied complete run state")

        def parse_one_event(value, *args, **kwargs):
            assert '"events"' not in value
            parsed_event_bodies.append(value)
            return original_loads(value, *args, **kwargs)

        with monkeypatch.context() as guarded:
            guarded.setattr(client_module, "deepcopy", reject_full_copy)
            guarded.setattr(client_module.json, "loads", parse_one_event)
            with provider._state_lock:
                status_run, _ = provider._apply_delta_envelope_locked(
                    RunDeltaEnvelope(
                        operator_instance_id="operator-1",
                        delta=RunDelta(
                            sequence=2,
                            change=RunStatusChanged(
                                "run-1",
                                RunStatus.RUNNING,
                                started_at=1.0,
                                revision=2,
                            ),
                        ),
                    )
                )
                assert initial.status is RunStatus.PENDING
                assert status_run.nodes is initial.nodes
                assert status_run.logs is initial.logs

                node_run, _ = provider._apply_delta_envelope_locked(
                    RunDeltaEnvelope(
                        operator_instance_id="operator-1",
                        delta=RunDelta(
                            sequence=3,
                            change=NodeStatusChanged(
                                "run-1",
                                "node-1",
                                NodeStatus.RUNNING,
                                started_at=1.0,
                                revision=3,
                            ),
                        ),
                    )
                )
                assert status_run.nodes["node-1"].status is NodeStatus.PENDING
                assert node_run.nodes is not status_run.nodes
                assert node_run.nodes["node-1"] is not status_run.nodes["node-1"]
                assert node_run.logs is status_run.logs

                appended_log = LogEntry(
                    timestamp=datetime(2026, 7, 22),
                    level=LogLevel.INFO,
                    node_id="node-1",
                    message="complete",
                )
                log_run, _ = provider._apply_delta_envelope_locked(
                    RunDeltaEnvelope(
                        operator_instance_id="operator-1",
                        delta=RunDelta(
                            sequence=4,
                            change=LogAppended(
                                "run-1",
                                LogRecordDescriptor(
                                    sequence=1,
                                    timestamp=appended_log.timestamp,
                                    level=appended_log.level,
                                    node_id=appended_log.node_id,
                                    size_bytes=len(appended_log.message),
                                    body_token="log-token",
                                ),
                            ),
                        ),
                    ),
                    log_detail=appended_log,
                )
                assert node_run.logs == []
                assert log_run.logs == []
                assert log_run.logs is node_run.logs
                assert provider._log_entries["run-1"] == [appended_log]
                assert log_run.nodes is node_run.nodes

                first_event_run = None
                for event_sequence in range(1, 501):
                    event_json = json.dumps(
                        {
                            "sequence": event_sequence,
                            "event_kind": "iteration.recorded",
                            "data": {"iteration": event_sequence},
                        }
                    )
                    event_run, _ = provider._apply_delta_envelope_locked(
                        RunDeltaEnvelope(
                            operator_instance_id="operator-1",
                            delta=RunDelta(
                                sequence=event_sequence + 4,
                                change=AgentEventAppended(
                                    "run-1",
                                    "node-1",
                                    AgentEventDescriptor(
                                        event_sequence=event_sequence,
                                        size_bytes=len(event_json),
                                        body_token=f"event-{event_sequence}",
                                    ),
                                ),
                            ),
                        ),
                        event_detail=AgentEvent(
                            event_sequence=event_sequence,
                            event_json=event_json,
                            size_bytes=len(event_json),
                        ),
                    )
                    if first_event_run is None:
                        first_event_run = event_run

        assert len(parsed_event_bodies) == 500
        assert provider._agent_events[key] is event_container
        assert len(event_container) == 500
        assert first_event_run is not None
        assert first_event_run.revision == 5
        assert provider._runs_by_id["run-1"].revision == 504
        assert first_event_run.nodes["node-1"].agent_trace_json is None
        with provider._state_lock:
            materialized = provider._materialize_run_locked(provider._runs_by_id["run-1"])
        assert len(json.loads(materialized.nodes["node-1"].agent_trace_json)["events"]) == 500
    finally:
        provider.close()


def test_callbacks_receive_isolated_values_without_cumulative_copies():
    provider = GrpcStateProvider("localhost:1")
    try:
        run, _ = provider._apply_delta_envelope(_created())
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


def test_hydrated_run_rejects_body_when_trace_descriptor_advanced():
    provider = GrpcStateProvider("localhost:1")
    key = ("run-1", "node-1")
    first_trace = TraceDescriptor(
        status="completed",
        revision=2,
        available=True,
        complete=True,
        event_count=1,
        size_bytes=20,
    )
    current_trace = dataclasses.replace(first_trace, revision=3, size_bytes=24)
    snapshot = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=2,
        summary=RunSummary(
            run_id="run-1",
            flow_name="flow",
            created_sequence=1,
            revision=2,
        ),
        nodes=(
            NodeSnapshot(
                node_id="node-1",
                name="Node",
                node_type="step",
                trace=first_trace,
                revision=2,
            ),
        ),
    )
    hydrated = RunState(
        run_id="run-1",
        flow_name="flow",
        operator_instance_id="operator-1",
        created_sequence=1,
        revision=2,
        nodes={
            "node-1": NodeState(
                node_id="node-1",
                name="Node",
                node_type="step",
                trace=first_trace,
                revision=2,
            )
        },
    )
    current = dataclasses.replace(
        hydrated,
        revision=3,
        nodes={
            "node-1": dataclasses.replace(
                hydrated.nodes["node-1"],
                trace=current_trace,
                revision=3,
            )
        },
    )
    provider._install_structural_baseline("operator-1", 3, {"run-1": current})
    provider._agent_events[key] = [{"sequence": 1}]
    provider._trace_bodies[key] = {"marker": "current"}
    provider._hydrated_agent_nodes.add(key)
    starting_cursor = provider._cursor

    try:
        with pytest.raises(_DetailHydrationRaceError, match="trace descriptor advanced"):
            provider._commit_hydrated_run(
                snapshot,
                hydrated,
                logs=[],
                starting_cursor=starting_cursor,
                hydrated_agent_nodes={key},
                agent_sequences={key: 1},
                agent_events={key: [{"sequence": 1}]},
                trace_bodies={key: {"marker": "stale"}},
            )
        retained = provider._runs_by_id["run-1"]
        assert retained.nodes["node-1"].trace.revision == 3
        assert provider._trace_bodies[key] == {"marker": "current"}
    finally:
        provider.close()


def test_client_reads_structural_state_from_state_detail_rpcs():
    provider = GrpcStateProvider("localhost:1")

    class StateStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            if request.page_size == 1000:
                assert request.workflow_selector == "flow.py::flow"
            else:
                assert request.page_size == 1
                assert request.workflow_selector == ""
            assert kwargs["timeout"] == provider._unary_timeout
            return pb.RunSummaryPage(
                operator_instance_id="operator-1",
                as_of_sequence=4,
                runs=[
                    pb.RunSummaryMsg(
                        run_id="run-1",
                        flow_name="flow",
                        status="running",
                        workflow_id="flow.py::flow",
                        created_sequence=1,
                        revision=4,
                    )
                ],
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            assert request.run_id == "run-1"
            assert request.operator_instance_id == "operator-1"
            assert request.as_of_sequence == 4
            assert kwargs["timeout"] == provider._unary_timeout
            return pb.RunSnapshotMsg(
                operator_instance_id="operator-1",
                as_of_sequence=4,
                summary=pb.RunSummaryMsg(
                    run_id="run-1",
                    flow_name="flow",
                    status="running",
                    workflow_id="flow.py::flow",
                    created_sequence=1,
                    revision=4,
                ),
                nodes=[
                    pb.NodeSnapshotMsg(
                        node_id="node-1",
                        name="Node",
                        node_type="step",
                        status="running",
                        revision=3,
                    )
                ],
                latest_log_sequence=2,
                log_page_token="logs-token",
            )

        def ListLogs(self, request, **kwargs):  # noqa: N802
            assert request.page_token == "logs-token"
            assert request.after_sequence == 0
            return pb.LogPage(
                operator_instance_id="operator-1",
                as_of_sequence=4,
                logs=[
                    pb.LogRecordDescriptorMsg(
                        sequence=sequence,
                        timestamp=float(sequence),
                        level="INFO",
                        node_id="node-1",
                        size_bytes=5,
                        body_token=f"log-{sequence}",
                    )
                    for sequence in (1, 2)
                ],
            )

        def ReadDetail(self, request, **kwargs):  # noqa: N802
            yield pb.DetailChunk(
                chunk_index=0,
                data=request.body_token.encode(),
                eof=True,
            )

    provider._stub = StateStub()
    try:
        summaries = provider.list_runs("flow.py::flow")
        snapshot = provider.get_run("run-1")
    finally:
        provider.close()

    assert len(summaries) == 1
    assert summaries[0].nodes == {}
    assert summaries[0].logs == []
    assert not summaries[0].details_hydrated
    assert snapshot.nodes["node-1"].status == NodeStatus.RUNNING
    assert [entry.message for entry in snapshot.logs] == ["log-1", "log-2"]
    assert snapshot.latest_log_sequence == 2
    assert snapshot.details_hydrated


def test_get_run_does_not_report_absence_when_client_epoch_changes():
    list_started = threading.Event()
    release_list = threading.Event()
    requests = []
    results = []
    errors = []
    provider = GrpcStateProvider("localhost:1")

    class SnapshotStub:
        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            list_started.set()
            if not release_list.wait(1):
                raise RuntimeError("baseline barrier timed out")
            return pb.RunSummaryPage(
                operator_instance_id="snapshot-operator",
                as_of_sequence=2,
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            requests.append(request)
            return pb.RunSnapshotMsg(
                operator_instance_id=request.operator_instance_id,
                as_of_sequence=request.as_of_sequence,
                summary=pb.RunSummaryMsg(
                    run_id=request.run_id,
                    flow_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=1,
                ),
            )

    provider._stub = SnapshotStub()
    provider._install_structural_baseline("old-operator", 99, {})

    def read_run():
        try:
            results.append(provider.get_run("run-1"))
        except Exception as error:
            errors.append(error)

    reader = threading.Thread(target=read_run)
    try:
        reader.start()
        assert list_started.wait(1)
        provider._install_structural_baseline("new-operator", 3, {})
        release_list.set()
        reader.join(1)
        assert not reader.is_alive()
    finally:
        release_list.set()
        reader.join(1)
        provider.close()

    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], _DetailHydrationRaceError)
    assert len(requests) == 3
    assert all(
        request.operator_instance_id == "snapshot-operator" and request.as_of_sequence == 2
        for request in requests
    )
    assert provider._cursor.operator_instance_id == "new-operator"
    assert provider._cursor.sequence == 3


def test_get_run_retries_when_pinned_baseline_expires():
    provider = GrpcStateProvider("localhost:1")

    class ExpiredBaseline(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.FAILED_PRECONDITION

        def details(self):
            return "baseline expired"

    class RacingStub:
        def __init__(self):
            self.summary_calls = 0
            self.snapshot_sequences = []

        def ListRunSummaries(self, request, **kwargs):  # noqa: N802
            self.summary_calls += 1
            return pb.RunSummaryPage(
                operator_instance_id="operator-1",
                as_of_sequence=self.summary_calls,
            )

        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            self.snapshot_sequences.append(request.as_of_sequence)
            if request.as_of_sequence == 1:
                raise ExpiredBaseline()
            return pb.RunSnapshotMsg(
                operator_instance_id=request.operator_instance_id,
                as_of_sequence=request.as_of_sequence,
                summary=pb.RunSummaryMsg(
                    run_id=request.run_id,
                    flow_name="flow",
                    status="running",
                    created_sequence=1,
                    revision=2,
                ),
            )

    stub = RacingStub()
    provider._stub = stub
    try:
        run = provider.get_run("run-1")
    finally:
        provider.close()

    assert run is not None
    assert run.status is RunStatus.RUNNING
    assert stub.summary_calls == 2
    assert stub.snapshot_sequences == [1, 2]


def test_client_installs_authoritative_baseline_before_resuming_deltas():
    provider = GrpcStateProvider("localhost:1")

    class ExactSnapshotStub:
        def GetRunSnapshot(self, request, **kwargs):  # noqa: N802
            assert request.run_id == "run-1"
            assert request.operator_instance_id == "operator-1"
            assert request.as_of_sequence == 3
            return pb.RunSnapshotMsg(
                operator_instance_id="operator-1",
                as_of_sequence=3,
                summary=pb.RunSummaryMsg(
                    run_id="run-1",
                    flow_name="flow",
                    status="success",
                    created_sequence=1,
                    revision=3,
                ),
            )

    baseline = RunState(
        run_id="run-1",
        flow_name="flow",
        status=RunStatus.SUCCESS,
        operator_instance_id="operator-1",
        created_sequence=1,
        revision=3,
    )
    load_calls = 0

    def load_authoritative_baseline(load_snapshot):
        nonlocal load_calls
        load_calls += 1
        snapshot = load_snapshot("run-1", "operator-1", 3)
        assert snapshot.operator_instance_id == "operator-1"
        assert snapshot.as_of_sequence == 3
        return "operator-1", 3, {baseline.run_id: baseline}

    provider._stub = ExactSnapshotStub()
    provider._load_authoritative_structural_baseline = load_authoritative_baseline
    try:
        provider._reload_structural_state()
        assert load_calls == 1
        assert provider._cursor.sequence == 3
        assert provider._runs_by_id["run-1"] is baseline

        run, _ = provider._apply_delta_envelope(
            RunDeltaEnvelope(
                operator_instance_id="operator-1",
                delta=RunDelta(
                    sequence=4,
                    change=RunStatusChanged(
                        "run-1",
                        RunStatus.FAILED,
                        revision=4,
                    ),
                ),
            )
        )
        assert run.status is RunStatus.FAILED
    finally:
        provider.close()


def test_client_resets_baseline_and_resumes_after_operator_restart():
    provider = GrpcStateProvider("localhost:1")
    received = []

    class RestartedStub:
        stream_calls = 0

        def StreamRunDeltas(self, request, *, metadata):  # noqa: N802
            assert metadata is None
            self.stream_calls += 1
            if self.stream_calls == 1:
                assert request.operator_instance_id == "old-operator"
                assert request.after_sequence == 99
                yield pb.RunDeltaEnvelope(
                    operator_instance_id="new-operator",
                    reset_required=pb.ResetRequired(
                        history_floor=1,
                        latest_sequence=2,
                    ),
                )
                return
            assert request.operator_instance_id == "new-operator"
            assert request.after_sequence == 2
            yield pb.RunDeltaEnvelope(
                operator_instance_id="new-operator",
                delta=pb.RunDelta(
                    sequence=3,
                    run_status_changed=pb.RunStatusChangedDelta(
                        run_id="run-1",
                        status="success",
                        ended_at=3.0,
                        revision=3,
                    ),
                ),
            )
            provider._stream_stop.set()

    baseline = RunState(
        run_id="run-1",
        flow_name="flow",
        status=RunStatus.RUNNING,
        operator_instance_id="new-operator",
        created_sequence=1,
        revision=2,
    )
    provider._reset_baseline_loader = lambda notice: ResetBaseline(
        generation=notice.generation,
        operator_instance_id="new-operator",
        as_of_sequence=2,
        workflows=(),
        runs_by_workflow={"flow": (baseline,)},
    )

    provider._stub = RestartedStub()
    provider._install_structural_baseline("old-operator", 99, {})
    provider._run_callbacks.append(lambda run: received.append((run.run_id, run.status)))

    def reconcile_reset(notice):
        reset_baseline = provider.load_reset_baseline(notice)
        provider.acknowledge_stream_reset(
            notice.generation,
            reset_baseline.operator_instance_id,
            reset_baseline.as_of_sequence,
        )

    provider.on_stream_reset(reconcile_reset)
    try:
        provider._stream_loop()
    finally:
        provider.close()

    assert received == [("run-1", RunStatus.SUCCESS)]
    assert provider._cursor.operator_instance_id == "new-operator"
    assert provider._cursor.sequence == 3
