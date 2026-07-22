from datetime import datetime

from runtime.operator.convert import (
    agent_event_from_proto,
    agent_event_to_proto,
    run_snapshot_from_proto,
    run_snapshot_to_proto,
    sequenced_log_entry_from_proto,
    sequenced_log_entry_to_proto,
)
from runtime.operator.models import (
    AgentEvent,
    LogEntry,
    LogLevel,
    NodeSnapshot,
    NodeStatus,
    RunSnapshot,
    RunStatus,
    RunSummary,
    SequencedLogEntry,
    TraceDescriptor,
)
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb


def test_structural_snapshot_contract_excludes_detail_bodies():
    snapshot_fields = pb.RunSnapshotMsg.DESCRIPTOR.fields_by_name
    node_fields = pb.NodeSnapshotMsg.DESCRIPTOR.fields_by_name
    summary_fields = pb.RunSummaryMsg.DESCRIPTOR.fields_by_name

    assert set(snapshot_fields) == {
        "operator_instance_id",
        "as_of_sequence",
        "summary",
        "nodes",
        "latest_log_sequence",
    }
    assert "logs" not in snapshot_fields
    assert "agent_trace_json" not in node_fields
    assert "logs" not in summary_fields
    assert "trace" not in summary_fields
    snapshot_request_fields = pb.GetRunSnapshotRequest.DESCRIPTOR.fields_by_name
    assert set(snapshot_request_fields) == {
        "run_id",
        "operator_instance_id",
        "as_of_sequence",
    }
    trace_request_fields = pb.ReadTraceRequest.DESCRIPTOR.fields_by_name
    assert set(trace_request_fields) == {
        "run_id",
        "node_id",
        "revision",
        "operator_instance_id",
    }


def test_snapshot_detail_cursor_and_descriptor_roundtrip():
    descriptor = TraceDescriptor(
        status="completed",
        revision=17,
        available=True,
        complete=True,
        event_count=42,
        size_bytes=5_000_000,
    )
    snapshot = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=23,
        summary=RunSummary(
            run_id="run-1",
            flow_name="example",
            status=RunStatus.RUNNING,
            workflow_id="flow.py::example",
            workflow_display_name="Example",
            created_sequence=2,
            revision=23,
        ),
        nodes=(
            NodeSnapshot(
                node_id="agent_1",
                name="Agent",
                node_type="step",
                status=NodeStatus.SUCCESS,
                trace=descriptor,
                revision=17,
            ),
        ),
        latest_log_sequence=22,
    )

    assert run_snapshot_from_proto(run_snapshot_to_proto(snapshot)) == snapshot


def test_detail_records_have_stable_sequences():
    log = SequencedLogEntry(
        sequence=12,
        entry=LogEntry(
            timestamp=datetime(2026, 7, 22, 12, 30),
            level=LogLevel.INFO,
            node_id="agent_1",
            message="complete",
        ),
    )
    event = AgentEvent(event_sequence=7, event_json='{"event_kind":"code.executed"}')

    assert sequenced_log_entry_from_proto(sequenced_log_entry_to_proto(log)) == log
    assert agent_event_from_proto(agent_event_to_proto(event)) == event


def test_delta_envelope_distinguishes_changes_from_reset():
    delta = pb.RunDeltaEnvelope(
        operator_instance_id="operator-1",
        delta=pb.RunDelta(
            sequence=24,
            node_status_changed=pb.NodeStatusChangedDelta(
                run_id="run-1",
                node_id="agent_1",
                status="success",
                revision=24,
            ),
        ),
    )
    reset = pb.RunDeltaEnvelope(
        operator_instance_id="operator-2",
        reset_required=pb.ResetRequired(history_floor=100, latest_sequence=200),
    )

    assert delta.WhichOneof("payload") == "delta"
    assert delta.delta.WhichOneof("change") == "node_status_changed"
    assert reset.WhichOneof("payload") == "reset_required"


def test_operator_identity_is_stable_and_distinguishes_restarts():
    first = Operator(watch=False, schedule=False)
    second = Operator(watch=False, schedule=False)
    try:
        assert first.operator_instance_id
        assert first.operator_instance_id == first.operator_instance_id
        assert first.operator_instance_id != second.operator_instance_id
        assert first.current_sequence == 0
    finally:
        first.close()
        second.close()


def test_service_exposes_parallel_workstream_contracts():
    methods = pb.DESCRIPTOR.services_by_name["OperatorService"].methods_by_name

    assert {
        "ListRunSummaries",
        "GetRunSnapshot",
        "ListLogs",
        "ListAgentEvents",
        "ReadTrace",
        "StreamRunDeltas",
    } <= set(methods)
    assert methods["ReadTrace"].server_streaming is True
    assert methods["StreamRunDeltas"].server_streaming is True
