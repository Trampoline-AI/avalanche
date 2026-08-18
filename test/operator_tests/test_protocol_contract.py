from google.protobuf.descriptor import FieldDescriptor

from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb


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
            not (field.name == "content" and field.type == FieldDescriptor.TYPE_BYTES)
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
        "retained_floor_event_ulid": 4,
        "event_ulid": 5,
    }

    watch_request_fields = pb.WatchRunStatusRequestV2.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in watch_request_fields.items()} == {
        "after_cursor": 1,
        "scope_ref": 2,
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
        "event_ulid": 1,
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
        retained_floor_event_ulid="00000000000000000000000007",
        event_ulid="0000000000000000000000000B",
    )
    for envelope in (
        pb.RunStatusEnvelopeV2(
            event_ulid=complete_cursor.event_ulid,
            cursor=complete_cursor,
            run_created=pb.RunCreatedV2(summary=pb.RunSummaryV2(run_id="run-1")),
        ),
        pb.RunStatusEnvelopeV2(
            event_ulid=complete_cursor.event_ulid,
            cursor=complete_cursor,
            run_status_changed=pb.RunStatusChangedV2(
                summary=pb.RunSummaryV2(run_id="run-1", status="running")
            ),
        ),
    ):
        assert envelope.cursor == complete_cursor
        assert envelope.cursor.event_ulid == envelope.event_ulid
        assert (
            pb.WatchRunStatusRequestV2(after_cursor=envelope.cursor).after_cursor
            == complete_cursor
        )


def test_legacy_operator_service_and_messages_are_absent():
    assert "OperatorService" not in pb.DESCRIPTOR.services_by_name
    assert "CatalogSnapshotMsg" not in pb.DESCRIPTOR.message_types_by_name
    assert "OperatorUpdateEnvelope" not in pb.DESCRIPTOR.message_types_by_name
