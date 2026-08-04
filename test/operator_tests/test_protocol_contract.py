from datetime import datetime

from runtime.operator.convert import (
    agent_event_descriptor_from_proto,
    agent_event_descriptor_to_proto,
    log_record_descriptor_from_proto,
    log_record_descriptor_to_proto,
    node_snapshot_from_proto,
    node_snapshot_to_proto,
    run_snapshot_from_proto,
    run_snapshot_to_proto,
)
from runtime.operator.models import (
    AgentEventDescriptor,
    LogLevel,
    LogRecordDescriptor,
    NodeSnapshot,
    NodeStatus,
    RunSnapshot,
    RunStatus,
    RunSummary,
    TraceDescriptor,
    TraceHeader,
    WorkflowTopology,
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
        "log_page_token",
        "topology",
    }
    assert "logs" not in snapshot_fields
    assert "agent_trace_json" not in node_fields
    assert "event_page_token" in node_fields
    assert "running_elapsed_seconds" in node_fields
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
    detail_request_fields = pb.ReadDetailRequest.DESCRIPTOR.fields_by_name
    assert set(detail_request_fields) == {"body_token"}


def test_snapshot_detail_cursor_and_descriptor_roundtrip():
    descriptor = TraceDescriptor(
        status="completed",
        revision=17,
        available=True,
        complete=True,
        event_count=42,
        size_bytes=5_000_000,
        latest_event_sequence=42,
        header=TraceHeader(
            status="completed",
            model="main",
            sub_model="sub",
            iterations=3,
            max_iterations=5,
            duration_ms=1250,
            usage_json='{"main":{"input_tokens":12}}',
            telemetry_json='{"trace_id":"trace-1"}',
        ),
    )
    snapshot = RunSnapshot(
        operator_instance_id="operator-1",
        as_of_sequence=23,
        summary=RunSummary(
            run_id="run-1",
            flow_name="example",
            status=RunStatus.RUNNING,
            triggered_at=1_704_067_200.0,
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
                status=NodeStatus.RUNNING,
                trace=descriptor,
                revision=17,
                started_at=10.0,
                running_elapsed_seconds=4.5,
                event_page_token="events-token",
            ),
        ),
        latest_log_sequence=22,
        log_page_token="logs-token",
        topology=WorkflowTopology(
            node_ids=("agent_1",),
            graph=(("agent_1", ()),),
            node_types=(("agent_1", "step"),),
            display_names=(("agent_1", "Agent"),),
            agent_field_schemas_json=(
                (
                    "agent_1",
                    '{"inputs":[],"outputs":[{"name":"answer","type":"str",'
                    '"description":""}]}',
                ),
            ),
        ),
    )

    assert run_snapshot_from_proto(run_snapshot_to_proto(snapshot)) == snapshot


def test_detail_records_expose_only_bounded_metadata():
    log = LogRecordDescriptor(
        sequence=12,
        timestamp=datetime(2026, 7, 22, 12, 30),
        level=LogLevel.INFO,
        node_id="agent_1",
        size_bytes=5_000_000,
        body_token="opaque-log-token",
    )
    event = AgentEventDescriptor(
        invocation_id="test-invocation",
        event_sequence=7,
        size_bytes=5_000_000,
        body_token="opaque-event-token",
        event_kind="iteration.recorded",
        iteration=3,
        duration_ms=1250,
        error=True,
        tool_count=2,
        predict_count=4,
    )
    failed = NodeSnapshot(
        node_id="failed",
        name="Failed",
        node_type="step",
        status=NodeStatus.FAILED,
        error="invalid customer record",
    )
    assert node_snapshot_from_proto(node_snapshot_to_proto(failed)) == failed

    assert log_record_descriptor_from_proto(log_record_descriptor_to_proto(log)) == log
    assert agent_event_descriptor_from_proto(agent_event_descriptor_to_proto(event)) == event
    assert "message" not in pb.LogRecordDescriptorMsg.DESCRIPTOR.fields_by_name
    assert "event_json" not in pb.AgentEventDescriptorMsg.DESCRIPTOR.fields_by_name


def test_update_envelope_distinguishes_changes_from_reset():
    update = pb.OperatorUpdateEnvelope(
        operator_instance_id="operator-1",
        update=pb.OperatorUpdate(
            sequence=24,
            node_status_changed=pb.NodeStatusChanged(
                run_id="run-1",
                node_id="agent_1",
                status="success",
                revision=24,
            ),
        ),
    )
    reset = pb.OperatorUpdateEnvelope(
        operator_instance_id="operator-2",
        reset_required=pb.ResetRequired(history_floor=100, latest_sequence=200),
    )

    assert update.WhichOneof("payload") == "update"
    assert update.update.WhichOneof("change") == "node_status_changed"
    assert reset.WhichOneof("payload") == "reset_required"


def test_run_update_is_a_sequenced_typed_change_record():
    update_fields = pb.OperatorUpdate.DESCRIPTOR.fields_by_name
    change_fields = pb.OperatorUpdate.DESCRIPTOR.oneofs_by_name["change"].fields
    envelope_fields = pb.OperatorUpdateEnvelope.DESCRIPTOR.fields_by_name
    payload_fields = pb.OperatorUpdateEnvelope.DESCRIPTOR.oneofs_by_name["payload"].fields

    assert {name: field.number for name, field in update_fields.items()} == {
        "sequence": 1,
        "run_created": 2,
        "run_status_changed": 3,
        "node_status_changed": 4,
        "log_appended": 5,
        "agent_event_appended": 6,
        "trace_finalized": 7,
        "catalog_replaced": 8,
    }
    assert [field.name for field in change_fields] == [
        "run_created",
        "run_status_changed",
        "node_status_changed",
        "log_appended",
        "agent_event_appended",
        "trace_finalized",
        "catalog_replaced",
    ]
    assert "run" not in update_fields
    assert {name: field.number for name, field in envelope_fields.items()} == {
        "operator_instance_id": 1,
        "update": 2,
        "reset_required": 3,
    }
    assert [field.name for field in payload_fields] == ["update", "reset_required"]


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
        "StreamOperatorUpdates",
    } <= set(methods)
    assert methods["ReadTrace"].server_streaming is True
    assert methods["StreamOperatorUpdates"].server_streaming is True
    assert methods["ReadDetail"].server_streaming is True


def test_legacy_full_state_rpcs_and_messages_are_absent():
    methods = pb.DESCRIPTOR.services_by_name["OperatorService"].methods_by_name
    messages = pb.DESCRIPTOR.message_types_by_name

    assert {"ListRuns", "GetRun", "StreamUpdates"}.isdisjoint(methods)
    assert {"RunStateMsg", "RunList"}.isdisjoint(messages)
