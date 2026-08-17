from datetime import datetime

from google.protobuf.descriptor import FieldDescriptor

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
        "workflow_reload_status": 9,
    }
    assert [field.name for field in change_fields] == [
        "run_created",
        "run_status_changed",
        "node_status_changed",
        "log_appended",
        "agent_event_appended",
        "trace_finalized",
        "catalog_replaced",
        "workflow_reload_status",
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


def test_legacy_operator_service_descriptor_and_key_fields_are_unchanged():
    service = pb.DESCRIPTOR.services_by_name["OperatorService"]
    expected_methods = {
        "GetCatalog": ("Empty", "CatalogSnapshotMsg", False),
        "StartRun": ("StartRunRequest", "StartRunResponse", False),
        "CancelRun": ("CancelRunRequest", "Empty", False),
        "GetRunResult": ("GetRunRequest", "RunResultMsg", False),
        "ListRunSummaries": (
            "ListRunSummariesRequest",
            "RunSummaryPage",
            False,
        ),
        "GetRunSnapshot": ("GetRunSnapshotRequest", "RunSnapshotMsg", False),
        "GetLatestRunSnapshot": (
            "GetLatestRunSnapshotRequest",
            "RunSnapshotMsg",
            False,
        ),
        "ListLogs": ("ListLogsRequest", "LogPage", False),
        "ListAgentEvents": ("ListAgentEventsRequest", "AgentEventPage", False),
        "ReadTrace": ("ReadTraceRequest", "TraceChunk", True),
        "ReadDetail": ("ReadDetailRequest", "DetailChunk", True),
        "StreamOperatorUpdates": (
            "StreamOperatorUpdatesRequest",
            "OperatorUpdateEnvelope",
            True,
        ),
    }

    assert [method.name for method in service.methods] == list(expected_methods)
    assert all(not method.client_streaming for method in service.methods)
    for method_name, (
        request_name,
        response_name,
        server_streaming,
    ) in expected_methods.items():
        method = service.methods_by_name[method_name]
        assert method.input_type.name == request_name
        assert method.output_type.name == response_name
        assert method.server_streaming is server_streaming

    assert {
        name: field.number
        for name, field in pb.StartRunRequest.DESCRIPTOR.fields_by_name.items()
    } == {
        "input_json": 2,
        "context_json": 3,
        "input_files": 4,
        "run_id": 6,
        "workflow_selector": 7,
    }
    assert {
        name: field.number
        for name, field in pb.ListRunSummariesRequest.DESCRIPTOR.fields_by_name.items()
    } == {
        "workflow_selector": 1,
        "page_size": 2,
        "page_token": 3,
    }
    assert {
        name: field.number
        for name, field in pb.GetRunSnapshotRequest.DESCRIPTOR.fields_by_name.items()
    } == {
        "run_id": 1,
        "operator_instance_id": 2,
        "as_of_sequence": 3,
    }


def test_v2_service_exposes_exact_native_job_contract():
    service = pb.DESCRIPTOR.services_by_name["OperatorServiceV2"]
    expected_methods = {
        "DiscoverFlows": ("DiscoverFlowsRequestV2", "FlowListV2", False),
        "StartRun": ("StartRunRequestV2", "StartRunResponseV2", False),
        "CancelRun": ("CancelRunRequestV2", "CancelRunResponseV2", False),
        "ListRunSummaries": (
            "ListRunSummariesRequestV2",
            "RunSummaryPageV2",
            False,
        ),
        "GetRunSnapshot": ("GetRunSnapshotRequestV2", "RunSnapshotV2", False),
        "ListRunActivity": ("ListRunActivityRequestV2", "RunActivityPageV2", False),
        "ReadActivityDetail": (
            "ReadActivityDetailRequestV2",
            "ActivityDetailChunkV2",
            True,
        ),
        "GetRunResult": ("GetRunResultRequestV2", "RunResultV2", False),
        "ListRunOutputArtifacts": (
            "ListRunOutputArtifactsRequestV2",
            "RunOutputArtifactPageV2",
            False,
        ),
        "ReadRunOutputArtifact": (
            "ReadRunOutputArtifactRequestV2",
            "RunOutputArtifactChunkV2",
            True,
        ),
        "WatchRunStatus": (
            "WatchRunStatusRequestV2",
            "RunStatusEnvelopeV2",
            True,
        ),
    }

    assert set(service.methods_by_name) == set(expected_methods)
    assert all(not method.client_streaming for method in service.methods)
    for method_name, (
        request_name,
        response_name,
        server_streaming,
    ) in expected_methods.items():
        method = service.methods_by_name[method_name]
        assert method.input_type.name == request_name
        assert method.output_type.name == response_name
        assert method.server_streaming is server_streaming

    assert {"ListAgentEvents", "ReadTrace"}.isdisjoint(service.methods_by_name)


def test_v2_activity_detail_reference_is_complete_and_bound():
    fields = pb.ActivityDetailRefV2.DESCRIPTOR.fields_by_name

    assert {name: field.number for name, field in fields.items()} == {
        "run_id": 1,
        "scope_ref": 2,
        "activity_id": 3,
        "run_sequence": 4,
        "object_uri": 5,
        "object_key": 6,
        "sha256": 7,
        "size_bytes": 8,
    }
    assert fields["scope_ref"].message_type.name == "ScopeReferenceV2"
    detail_request_fields = pb.ReadActivityDetailRequestV2.DESCRIPTOR.fields_by_name
    assert set(detail_request_fields) == {"detail_ref"}
    assert detail_request_fields["detail_ref"].message_type.name == "ActivityDetailRefV2"

    chunk_fields = pb.ActivityDetailChunkV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in chunk_fields.items()} == {
        "chunk_index": 1,
        "data": 2,
        "eof": 3,
    }
    assert chunk_fields["data"].type == FieldDescriptor.TYPE_BYTES


def test_v2_start_request_is_idempotent_and_uses_attachment_descriptors():
    start_fields = pb.StartRunRequestV2.DESCRIPTOR.fields_by_name

    assert {name: field.number for name, field in start_fields.items()} == {
        "run_id": 1,
        "workflow_selector": 2,
        "input_json": 3,
        "context_json": 4,
        "input_files": 5,
    }
    assert "flow_name" not in start_fields
    assert start_fields["input_files"].message_type.name == "FileAttachmentV2"

    attachment_fields = pb.FileAttachmentV2.DESCRIPTOR.fields_by_name
    assert {"attachment_id", "object_uri", "object_key", "sha256", "size_bytes"} <= set(
        attachment_fields
    )
    assert "content" not in attachment_fields


def test_v2_result_and_artifact_bodies_are_streamed_not_unary_content():
    service = pb.DESCRIPTOR.services_by_name["OperatorServiceV2"]
    unary_output_messages = [
        method.output_type for method in service.methods if not method.server_streaming
    ]

    for descriptor in unary_output_messages:
        assert all(
            not (
                field.name == "content" and field.type == FieldDescriptor.TYPE_BYTES
            )
            for field in descriptor.fields
        )

    for descriptor in (
        pb.RunResultV2.DESCRIPTOR,
        pb.ResultValueV2.DESCRIPTOR,
        pb.ResultFileDescriptorV2.DESCRIPTOR,
        pb.RunOutputArtifactPageV2.DESCRIPTOR,
        pb.RunOutputArtifactDescriptorV2.DESCRIPTOR,
        pb.RunOutputArtifactRefV2.DESCRIPTOR,
    ):
        assert all(field.type != FieldDescriptor.TYPE_BYTES for field in descriptor.fields)

    artifact_read = service.methods_by_name["ReadRunOutputArtifact"]
    assert artifact_read.server_streaming is True
    artifact_chunk_fields = pb.RunOutputArtifactChunkV2.DESCRIPTOR.fields_by_name
    assert artifact_chunk_fields["data"].type == FieldDescriptor.TYPE_BYTES
    assert {"chunk_index", "eof"} <= set(artifact_chunk_fields)


def test_v2_cursors_and_resets_require_complete_bounded_replay_state():
    cursor_fields = pb.LifecycleCursorV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in cursor_fields.items()} == {
        "stream": 1,
        "topology_fingerprint": 2,
        "stream_generation": 3,
        "retained_floor": 4,
        "source_sequence": 5,
    }

    watch_request_fields = pb.WatchRunStatusRequestV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in watch_request_fields.items()} == {
        "after_cursor": 1,
    }
    assert watch_request_fields["after_cursor"].message_type.name == "LifecycleCursorV2"

    continuation_fields = pb.ContinuationRefV2.DESCRIPTOR.fields_by_name
    assert set(continuation_fields) == {"scope_ref", "continuation_id", "cursor"}
    assert continuation_fields["scope_ref"].message_type.name == "ScopeReferenceV2"
    assert continuation_fields["cursor"].message_type.name == "LifecycleCursorV2"

    for descriptor in (
        pb.DiscoverFlowsRequestV2.DESCRIPTOR,
        pb.ListRunSummariesRequestV2.DESCRIPTOR,
        pb.ListRunActivityRequestV2.DESCRIPTOR,
        pb.ListRunOutputArtifactsRequestV2.DESCRIPTOR,
    ):
        assert descriptor.fields_by_name["page_size"].type == FieldDescriptor.TYPE_UINT32
        continuation = descriptor.fields_by_name["continuation"]
        assert continuation.message_type.name == "ContinuationRefV2"

    for message in pb.DESCRIPTOR.message_types_by_name.values():
        if message.name.endswith("V2"):
            assert "page_token" not in message.fields_by_name
            assert "body_token" not in message.fields_by_name
            assert "organization_id" not in message.fields_by_name
            assert "project_id" not in message.fields_by_name

    reset_fields = pb.ResetRequiredV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in reset_fields.items()} == {
        "history_floor": 1,
        "latest_cursor": 2,
    }
    assert all(
        field.message_type.name == "LifecycleCursorV2" for field in reset_fields.values()
    )

    status_fields = pb.RunStatusEnvelopeV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in status_fields.items()} == {
        "source_sequence": 1,
        "run_created": 2,
        "run_status_changed": 3,
        "reset_required": 4,
        "cursor": 5,
        "node_status_changed": 6,
        "activity_appended": 7,
        "flow_list_changed": 8,
        "flow_reload_status": 9,
        "scope_ref": 10,
    }
    assert status_fields["cursor"].message_type.name == "LifecycleCursorV2"
    assert status_fields["cursor"].containing_oneof is None
    payload_fields = pb.RunStatusEnvelopeV2.DESCRIPTOR.oneofs_by_name["payload"].fields
    assert [field.name for field in payload_fields] == [
        "run_created",
        "run_status_changed",
        "reset_required",
        "node_status_changed",
        "activity_appended",
        "flow_list_changed",
        "flow_reload_status",
    ]

    complete_cursor = pb.LifecycleCursorV2(
        stream="run-status",
        topology_fingerprint="topology-1",
        stream_generation=4,
        retained_floor=7,
        source_sequence=11,
    )
    for envelope in (
        pb.RunStatusEnvelopeV2(
            source_sequence=complete_cursor.source_sequence,
            cursor=complete_cursor,
            run_created=pb.RunCreatedV2(summary=pb.RunSummaryV2(run_id="run-1")),
        ),
        pb.RunStatusEnvelopeV2(
            source_sequence=complete_cursor.source_sequence,
            cursor=complete_cursor,
            run_status_changed=pb.RunStatusChangedV2(
                summary=pb.RunSummaryV2(run_id="run-1", status="running")
            ),
        ),
    ):
        assert envelope.cursor == complete_cursor
        assert envelope.cursor.source_sequence == envelope.source_sequence
        assert (
            pb.WatchRunStatusRequestV2(after_cursor=envelope.cursor).after_cursor
            == complete_cursor
        )


def test_legacy_full_state_rpcs_and_messages_are_absent():
    methods = pb.DESCRIPTOR.services_by_name["OperatorService"].methods_by_name
    messages = pb.DESCRIPTOR.message_types_by_name

    assert {"ListRuns", "GetRun", "StreamUpdates"}.isdisjoint(methods)
    assert {"RunStateMsg", "RunList"}.isdisjoint(messages)
