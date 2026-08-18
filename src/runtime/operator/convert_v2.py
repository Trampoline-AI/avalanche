"""Conversion between Python operator models and V2 protobuf messages."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime

from .models import (
    AgentEventAppended,
    AgentEventDescriptor,
    CatalogReplaced,
    CatalogSnapshot,
    LogAppended,
    LogLevel,
    LogRecordDescriptor,
    NodeSnapshot,
    NodeStatus,
    NodeStatusChanged,
    OperatorUpdate,
    OperatorUpdateEnvelope,
    ResetRequired,
    RunCreated,
    RunSnapshot,
    RunStatus,
    RunStatusChanged,
    RunSummary,
    ScanTargetInfo,
    TraceDescriptor,
    TraceFinalized,
    TraceHeader,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
    WorkflowReloadStatus,
    WorkflowTopology,
)
from .proto import operator_pb2 as pb

DETAIL_URI_SCHEME = "local://detail/"
TRACE_URI_SCHEME = "local://trace/"
RESULT_URI_SCHEME = "local://result/"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workflow_topology_to_v2(topology: WorkflowTopology) -> pb.WorkflowTopologyV2:
    return pb.WorkflowTopologyV2(
        node_ids=topology.node_ids,
        graph={
            parent: pb.NodeEdgesV2(children=children) for parent, children in topology.graph
        },
        node_types=dict(topology.node_types),
        display_names=dict(topology.display_names),
        agent_field_schemas_json=dict(topology.agent_field_schemas_json),
        agent_instruction_lines=dict(topology.agent_instruction_lines),
    )


def workflow_info_to_v2(info: WorkflowInfo) -> pb.FlowInfoV2:
    topology = WorkflowTopology(
        node_ids=tuple(info.node_ids),
        graph=tuple((parent, tuple(children)) for parent, children in info.graph.items()),
        node_types=tuple(sorted(info.node_types.items())),
        display_names=tuple(sorted(info.display_names.items())),
    )
    manifest_digest = sha256_hex(("\n".join([info.selector, *info.node_ids])).encode("utf-8"))
    return pb.FlowInfoV2(
        workflow_selector=info.selector,
        display_name=info.rendered_name,
        manifest_digest=manifest_digest,
        node_ids=info.node_ids,
        workflow_id=info.workflow_id or info.name,
        file_path=info.source_file,
        topology=workflow_topology_to_v2(topology),
        agent_node_ids=info.agent_node_ids,
        agent_metadata_json=info.agent_metadata_json,
        cron=info.cron or "",
        next_run_at=info.next_run_at or 0.0,
        last_run_at=info.last_run_at or 0.0,
        webhook_path=info.webhook_path or "",
        webhook_url=info.webhook_url or "",
        webhook_active=info.webhook_active,
    )


def scan_target_to_v2(target: ScanTargetInfo) -> pb.ScanTargetV2:
    return pb.ScanTargetV2(
        alias=target.alias,
        target_path=target.target_path,
        kind=target.kind,
    )


def discovery_diagnostic_to_v2(
    diagnostic: WorkflowDiscoveryDiagnostic,
) -> pb.DiscoveryDiagnosticV2:
    return pb.DiscoveryDiagnosticV2(
        path=diagnostic.path,
        kind=diagnostic.kind,
        message=diagnostic.message,
    )


def flow_list_to_v2(
    catalog: CatalogSnapshot,
    *,
    cursor: pb.LifecycleCursorV2,
    flows: list[pb.FlowInfoV2] | None = None,
    next_page: pb.ContinuationRefV2 | None = None,
    scope_ref: pb.ScopeReferenceV2 | None = None,
) -> pb.FlowListV2:
    message = pb.FlowListV2(
        cursor=cursor,
        revision=catalog.revision,
        flows=flows
        if flows is not None
        else [workflow_info_to_v2(w) for w in catalog.workflows],
        scan_targets=[scan_target_to_v2(item) for item in catalog.scan_targets],
        diagnostics=[discovery_diagnostic_to_v2(item) for item in catalog.diagnostics],
    )
    if next_page is not None:
        message.next_page.CopyFrom(next_page)
    if scope_ref is not None:
        message.scope_ref.CopyFrom(scope_ref)
    return message


def run_summary_to_v2(summary) -> pb.RunSummaryV2:
    return pb.RunSummaryV2(
        run_id=summary.run_id,
        workflow_selector=summary.workflow_id or summary.flow_name,
        workflow_display_name=summary.workflow_display_name or summary.flow_name,
        status=summary.status.value,
        started_at=summary.started_at or 0.0,
        ended_at=summary.ended_at or 0.0,
        created_sequence=summary.created_sequence,
        revision=summary.revision,
        triggered_by=summary.triggered_by,
        triggered_at=summary.triggered_at or 0.0,
    )


def trace_header_to_v2(header: TraceHeader) -> pb.TraceHeaderV2:
    message = pb.TraceHeaderV2(
        status=header.status,
        model=header.model,
        iterations=header.iterations,
        max_iterations=header.max_iterations,
        duration_ms=header.duration_ms,
        usage_json=header.usage_json,
    )
    if header.sub_model is not None:
        message.sub_model = header.sub_model
    if header.telemetry_json is not None:
        message.telemetry_json = header.telemetry_json
    return message


def trace_detail_ref_to_v2(
    run_id: str,
    node_id: str,
    descriptor: TraceDescriptor,
    run_sequence: int,
    scope_ref: pb.ScopeReferenceV2,
    sha256: str,
) -> pb.ActivityDetailRefV2:
    """Bind one immutable trace body by run, node, and descriptor revision."""
    return pb.ActivityDetailRefV2(
        run_id=run_id,
        scope_ref=scope_ref,
        activity_id=f"trace:{node_id}:{descriptor.revision}",
        run_sequence=run_sequence,
        object_uri=f"{TRACE_URI_SCHEME}{run_id}/{node_id}/{descriptor.revision}",
        object_key=f"{run_id}/{node_id}/{descriptor.revision}",
        sha256=sha256,
        size_bytes=descriptor.size_bytes,
    )


def trace_descriptor_to_v2(
    descriptor: TraceDescriptor,
    *,
    detail_ref: pb.ActivityDetailRefV2 | None = None,
) -> pb.TraceDescriptorV2:
    message = pb.TraceDescriptorV2(
        status=descriptor.status,
        revision=descriptor.revision,
        available=descriptor.available,
        complete=descriptor.complete,
        event_count=descriptor.event_count,
        size_bytes=descriptor.size_bytes,
        latest_event_sequence=descriptor.latest_event_sequence,
    )
    if descriptor.header is not None:
        message.header.CopyFrom(trace_header_to_v2(descriptor.header))
    if descriptor.available:
        if detail_ref is None:
            raise ValueError("available trace descriptor requires a canonical detail reference")
        message.detail_ref.CopyFrom(detail_ref)
    return message


def node_snapshot_to_v2(
    node: NodeSnapshot,
    *,
    trace_detail_ref: pb.ActivityDetailRefV2 | None = None,
    activity_continuation: pb.ContinuationRefV2 | None = None,
) -> pb.NodeSnapshotV2:
    message = pb.NodeSnapshotV2(
        node_id=node.node_id,
        name=node.name,
        node_type=node.node_type,
        status=node.status.value,
        started_at=node.started_at or 0.0,
        ended_at=node.ended_at or 0.0,
        revision=node.revision,
    )
    if node.running_elapsed_seconds is not None:
        message.running_elapsed_seconds = node.running_elapsed_seconds
    if node.error is not None:
        message.error = node.error
    if node.trace is not None:
        message.trace.CopyFrom(trace_descriptor_to_v2(node.trace, detail_ref=trace_detail_ref))
    if activity_continuation is not None:
        message.activity_continuation.CopyFrom(activity_continuation)
    return message


def run_snapshot_to_v2(
    snapshot: RunSnapshot,
    *,
    cursor: pb.LifecycleCursorV2,
    scope_ref: pb.ScopeReferenceV2,
    trace_detail_ref_for: Callable[[str, str, TraceDescriptor, int], pb.ActivityDetailRefV2],
    activity_continuations: Mapping[str, pb.ContinuationRefV2],
    log_continuation: pb.ContinuationRefV2 | None,
) -> pb.RunSnapshotV2:
    return pb.RunSnapshotV2(
        cursor=cursor,
        scope_ref=scope_ref,
        summary=run_summary_to_v2(snapshot.summary),
        nodes=[
            node_snapshot_to_v2(
                node,
                trace_detail_ref=(
                    trace_detail_ref_for(
                        snapshot.summary.run_id,
                        node.node_id,
                        node.trace,
                        node.trace.revision,
                    )
                    if node.trace is not None and node.trace.available
                    else None
                ),
                activity_continuation=activity_continuations.get(node.node_id),
            )
            for node in snapshot.nodes
        ],
        topology=workflow_topology_to_v2(snapshot.topology),
        latest_log_sequence=snapshot.latest_log_sequence,
        log_continuation=log_continuation,
    )


def body_detail_ref_to_v2(
    run_id: str,
    activity_id: str,
    body_token: str,
    run_sequence: int,
    size_bytes: int,
    scope_ref: pb.ScopeReferenceV2,
    sha256: str,
) -> pb.ActivityDetailRefV2:
    """Bind one retained detail body by its snapshot-issued token."""
    return pb.ActivityDetailRefV2(
        run_id=run_id,
        scope_ref=scope_ref,
        activity_id=activity_id,
        run_sequence=run_sequence,
        object_uri=f"{DETAIL_URI_SCHEME}{body_token}",
        object_key=body_token,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def log_activity_to_v2(
    log: LogRecordDescriptor,
    *,
    run_id: str,
    detail_ref: pb.ActivityDetailRefV2,
) -> pb.RunActivityDescriptorV2:
    return pb.RunActivityDescriptorV2(
        activity_id=f"log:{log.sequence}",
        run_sequence=log.sequence,
        kind="log",
        timestamp=log.timestamp.timestamp(),
        size_bytes=log.size_bytes,
        detail_ref=detail_ref,
        node_id=log.node_id,
        level=log.level.value,
    )


def agent_event_activity_to_v2(
    event: AgentEventDescriptor,
    *,
    run_id: str,
    node_id: str,
    detail_ref: pb.ActivityDetailRefV2,
) -> pb.RunActivityDescriptorV2:
    activity_id = f"agent:{node_id}:{event.event_sequence}"
    message = pb.RunActivityDescriptorV2(
        activity_id=activity_id,
        run_sequence=event.event_sequence,
        kind="agent_event",
        size_bytes=event.size_bytes,
        detail_ref=detail_ref,
        node_id=node_id,
        invocation_id=event.invocation_id,
        error=event.error,
        tool_count=event.tool_count,
        predict_count=event.predict_count,
        event_kind=event.event_kind,
    )
    if event.iteration is not None:
        message.iteration = event.iteration
    if event.duration_ms is not None:
        message.duration_ms = event.duration_ms
    return message


def trace_activity_to_v2(
    trace: TraceDescriptor,
    *,
    run_id: str,
    node_id: str,
    detail_ref: pb.ActivityDetailRefV2 | None,
) -> pb.RunActivityDescriptorV2:
    message = pb.RunActivityDescriptorV2(
        activity_id=f"trace:{node_id}:{trace.revision}",
        run_sequence=detail_ref.run_sequence if detail_ref is not None else 0,
        kind="trace",
        size_bytes=trace.size_bytes,
        node_id=node_id,
        trace=trace_descriptor_to_v2(trace, detail_ref=detail_ref),
    )
    if detail_ref is not None:
        message.detail_ref.CopyFrom(detail_ref)
    return message


def update_envelope_to_v2(
    envelope: OperatorUpdateEnvelope,
    *,
    scope_ref: pb.ScopeReferenceV2,
    cursor_for: Callable[[int], pb.LifecycleCursorV2],
    activity_continuation_for: Callable[[str, str, str], pb.ContinuationRefV2],
    body_detail_ref_for: Callable[[str, str, str, int, int], pb.ActivityDetailRefV2],
    trace_detail_ref_for: Callable[[str, str, TraceDescriptor, int], pb.ActivityDetailRefV2],
) -> pb.RunStatusEnvelopeV2:
    """Convert one operator update envelope with its complete event cursor."""
    message = pb.RunStatusEnvelopeV2()
    if envelope.reset_required is not None:
        history_floor = cursor_for(envelope.reset_required.history_floor)
        latest_cursor = cursor_for(envelope.reset_required.latest_sequence)
        message.reset_required.CopyFrom(
            pb.ResetRequiredV2(
                history_floor=history_floor,
                latest_cursor=latest_cursor,
            )
        )
        message.cursor.CopyFrom(latest_cursor)
        message.event_ulid = latest_cursor.event_ulid
        message.scope_ref.CopyFrom(scope_ref)
        return message

    update = envelope.update
    if update is None:
        raise ValueError("operator update envelope requires a payload")
    change = update.change
    cursor = cursor_for(update.sequence)
    message.event_ulid = cursor.event_ulid
    message.cursor.CopyFrom(cursor)
    message.scope_ref.CopyFrom(scope_ref)
    if isinstance(change, RunCreated):
        message.run_created.CopyFrom(
            pb.RunCreatedV2(
                summary=run_summary_to_v2(change.summary),
                nodes=[
                    node_snapshot_to_v2(
                        node,
                        trace_detail_ref=(
                            trace_detail_ref_for(
                                change.summary.run_id,
                                node.node_id,
                                node.trace,
                                change.summary.created_sequence,
                            )
                            if node.trace is not None and node.trace.available
                            else None
                        ),
                        activity_continuation=(
                            activity_continuation_for(
                                change.summary.run_id,
                                node.node_id,
                                node.event_page_token,
                            )
                            if node.event_page_token
                            else None
                        ),
                    )
                    for node in change.nodes
                ],
                topology=workflow_topology_to_v2(change.topology),
            )
        )
    elif isinstance(change, RunStatusChanged):
        message.run_status_changed.CopyFrom(
            pb.RunStatusChangedV2(
                summary=pb.RunSummaryV2(
                    run_id=change.run_id,
                    status=change.status.value,
                    started_at=change.started_at or 0.0,
                    ended_at=change.ended_at or 0.0,
                    revision=change.revision,
                )
            )
        )
    elif isinstance(change, NodeStatusChanged):
        message.node_status_changed.CopyFrom(
            pb.NodeStatusChangedV2(
                run_id=change.run_id,
                node=node_snapshot_to_v2(
                    NodeSnapshot(
                        node_id=change.node_id,
                        name=change.node_id,
                        node_type="",
                        status=change.status,
                        started_at=change.started_at,
                        ended_at=change.ended_at,
                        error=change.error,
                        revision=change.revision,
                        running_elapsed_seconds=change.running_elapsed_seconds,
                    ),
                ),
            )
        )
    elif isinstance(change, LogAppended):
        message.activity_appended.CopyFrom(
            pb.ActivityAppendedV2(
                run_id=change.run_id,
                activity=log_activity_to_v2(
                    change.log,
                    run_id=change.run_id,
                    detail_ref=body_detail_ref_for(
                        change.run_id,
                        f"log:{change.log.sequence}",
                        change.log.body_token,
                        change.log.sequence,
                        change.log.size_bytes,
                    ),
                ),
            )
        )
    elif isinstance(change, AgentEventAppended):
        message.activity_appended.CopyFrom(
            pb.ActivityAppendedV2(
                run_id=change.run_id,
                activity=agent_event_activity_to_v2(
                    change.event,
                    run_id=change.run_id,
                    node_id=change.node_id,
                    detail_ref=body_detail_ref_for(
                        change.run_id,
                        f"agent:{change.node_id}:{change.event.event_sequence}",
                        change.event.body_token,
                        change.event.event_sequence,
                        change.event.size_bytes,
                    ),
                ),
            )
        )
    elif isinstance(change, TraceFinalized):
        message.activity_appended.CopyFrom(
            pb.ActivityAppendedV2(
                run_id=change.run_id,
                activity=trace_activity_to_v2(
                    change.trace,
                    run_id=change.run_id,
                    node_id=change.node_id,
                    detail_ref=trace_detail_ref_for(
                        change.run_id,
                        change.node_id,
                        change.trace,
                        update.sequence,
                    )
                    if change.trace.available
                    else None,
                ),
            )
        )
    elif isinstance(change, CatalogReplaced):
        catalog_cursor = cursor_for(change.catalog.as_of_sequence)
        message.flow_list_changed.CopyFrom(
            pb.FlowListChangedV2(
                flow_list=flow_list_to_v2(
                    change.catalog, cursor=catalog_cursor, scope_ref=scope_ref
                )
            )
        )
    elif isinstance(change, WorkflowReloadStatus):
        message.flow_reload_status.CopyFrom(pb.FlowReloadStatusV2(reloading=change.reloading))
    else:
        raise TypeError(f"Unsupported operator update change: {type(change).__name__}")
    return message


# ── V2 proto → domain models (client side) ──────────────


def workflow_topology_from_v2(msg: pb.WorkflowTopologyV2) -> WorkflowTopology:
    node_ids = tuple(msg.node_ids)
    return WorkflowTopology(
        node_ids=node_ids,
        graph=tuple((node_id, tuple(msg.graph[node_id].children)) for node_id in node_ids),
        node_types=tuple((node_id, msg.node_types[node_id]) for node_id in node_ids),
        display_names=tuple((node_id, msg.display_names[node_id]) for node_id in node_ids),
        agent_field_schemas_json=tuple(
            (node_id, msg.agent_field_schemas_json[node_id])
            for node_id in node_ids
            if node_id in msg.agent_field_schemas_json
        ),
        agent_instruction_lines=tuple(
            (node_id, msg.agent_instruction_lines[node_id])
            for node_id in node_ids
            if node_id in msg.agent_instruction_lines
        ),
    )


def workflow_info_from_v2(msg: pb.FlowInfoV2) -> WorkflowInfo:
    node_ids = list(msg.topology.node_ids) or list(msg.node_ids)
    graph = {parent: list(edges.children) for parent, edges in msg.topology.graph.items()}
    return WorkflowInfo(
        name=msg.display_name,
        file_path=msg.file_path,
        node_ids=node_ids,
        graph=graph,
        node_types=dict(msg.topology.node_types),
        display_names=dict(msg.topology.display_names),
        agent_node_ids=list(msg.agent_node_ids),
        agent_metadata_json=dict(msg.agent_metadata_json),
        cron=msg.cron or None,
        next_run_at=msg.next_run_at or None,
        last_run_at=msg.last_run_at or None,
        workflow_id=msg.workflow_id or msg.workflow_selector,
        display_name=msg.display_name,
        relative_file=msg.file_path,
        webhook_path=msg.webhook_path or None,
        webhook_url=msg.webhook_url or None,
        webhook_active=msg.webhook_active,
    )


def scan_target_from_v2(msg: pb.ScanTargetV2) -> ScanTargetInfo:
    if msg.kind not in {"file", "directory"}:
        raise ValueError(f"Unknown scan target kind: {msg.kind}")
    return ScanTargetInfo(
        alias=msg.alias,
        target_path=msg.target_path,
        kind=msg.kind,
    )


def discovery_diagnostic_from_v2(
    msg: pb.DiscoveryDiagnosticV2,
) -> WorkflowDiscoveryDiagnostic:
    return WorkflowDiscoveryDiagnostic(
        path=msg.path,
        kind=msg.kind,
        message=msg.message,
    )


def catalog_snapshot_from_v2(
    msg: pb.FlowListV2,
    *,
    flows: list[pb.FlowInfoV2] | None = None,
) -> CatalogSnapshot:
    return CatalogSnapshot(
        revision=msg.revision,
        operator_instance_id=msg.scope_ref.reference,
        as_of_event_ulid=msg.cursor.event_ulid,
        workflows=tuple(
            workflow_info_from_v2(item) for item in (flows if flows is not None else msg.flows)
        ),
        scan_targets=tuple(scan_target_from_v2(item) for item in msg.scan_targets),
        diagnostics=tuple(discovery_diagnostic_from_v2(item) for item in msg.diagnostics),
    )


def run_summary_from_v2(msg: pb.RunSummaryV2) -> RunSummary:
    return RunSummary(
        run_id=msg.run_id,
        flow_name=msg.workflow_selector.rsplit("::", 1)[-1] or msg.workflow_display_name,
        status=RunStatus(msg.status),
        started_at=msg.started_at or None,
        ended_at=msg.ended_at or None,
        triggered_at=msg.triggered_at or None,
        triggered_by=msg.triggered_by or "manual",
        workflow_id=msg.workflow_selector,
        workflow_display_name=msg.workflow_display_name,
        created_sequence=msg.created_sequence,
        revision=msg.revision,
    )


def trace_header_from_v2(msg: pb.TraceHeaderV2) -> TraceHeader:
    return TraceHeader(
        status=msg.status,
        model=msg.model,
        sub_model=msg.sub_model if msg.HasField("sub_model") else None,
        iterations=msg.iterations,
        max_iterations=msg.max_iterations,
        duration_ms=msg.duration_ms,
        usage_json=msg.usage_json,
        telemetry_json=msg.telemetry_json if msg.HasField("telemetry_json") else None,
    )


def trace_descriptor_from_v2(msg: pb.TraceDescriptorV2) -> TraceDescriptor:
    return TraceDescriptor(
        status=msg.status,
        revision=msg.revision,
        available=msg.available,
        complete=msg.complete,
        event_count=msg.event_count,
        size_bytes=msg.size_bytes,
        latest_event_sequence=msg.latest_event_sequence,
        header=trace_header_from_v2(msg.header) if msg.HasField("header") else None,
    )


def node_snapshot_from_v2(msg: pb.NodeSnapshotV2) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=msg.node_id,
        name=msg.name,
        node_type=msg.node_type,
        status=NodeStatus(msg.status),
        started_at=msg.started_at or None,
        ended_at=msg.ended_at or None,
        error=msg.error if msg.HasField("error") else None,
        trace=trace_descriptor_from_v2(msg.trace) if msg.HasField("trace") else None,
        revision=msg.revision,
        event_page_token=msg.activity_continuation.continuation_id,
        running_elapsed_seconds=(
            msg.running_elapsed_seconds if msg.HasField("running_elapsed_seconds") else None
        ),
    )


def run_snapshot_from_v2(msg: pb.RunSnapshotV2) -> RunSnapshot:
    return RunSnapshot(
        operator_instance_id=msg.scope_ref.reference,
        as_of_sequence=0,
        summary=run_summary_from_v2(msg.summary),
        as_of_event_ulid=msg.cursor.event_ulid,
        nodes=tuple(node_snapshot_from_v2(node) for node in msg.nodes),
        latest_log_sequence=msg.latest_log_sequence,
        log_page_token=msg.log_continuation.continuation_id,
        topology=workflow_topology_from_v2(msg.topology),
    )


def log_record_descriptor_from_v2(
    msg: pb.RunActivityDescriptorV2,
) -> LogRecordDescriptor:
    return LogRecordDescriptor(
        sequence=msg.run_sequence,
        timestamp=datetime.fromtimestamp(msg.timestamp),
        level=LogLevel(msg.level),
        node_id=msg.node_id,
        size_bytes=msg.size_bytes,
        body_token=msg.detail_ref.object_key,
    )


def agent_event_descriptor_from_v2(
    msg: pb.RunActivityDescriptorV2,
) -> AgentEventDescriptor:
    return AgentEventDescriptor(
        invocation_id=msg.invocation_id,
        event_sequence=msg.run_sequence,
        size_bytes=msg.size_bytes,
        body_token=msg.detail_ref.object_key,
        event_kind=msg.event_kind,
        iteration=msg.iteration if msg.HasField("iteration") else None,
        duration_ms=msg.duration_ms if msg.HasField("duration_ms") else None,
        error=msg.error,
        tool_count=msg.tool_count,
        predict_count=msg.predict_count,
    )


def operator_update_envelope_from_v2(
    msg: pb.RunStatusEnvelopeV2,
) -> OperatorUpdateEnvelope:
    instance = msg.scope_ref.reference
    payload = msg.WhichOneof("payload")
    if payload == "reset_required":
        return OperatorUpdateEnvelope(
            operator_instance_id=instance,
            reset_required=ResetRequired(
                history_floor_event_ulid=msg.reset_required.history_floor.event_ulid,
                latest_event_ulid=msg.reset_required.latest_cursor.event_ulid,
            ),
        )
    event_ulid = msg.event_ulid
    if payload == "run_created":
        created = msg.run_created
        change = RunCreated(
            summary=run_summary_from_v2(created.summary),
            nodes=tuple(node_snapshot_from_v2(node) for node in created.nodes),
            topology=workflow_topology_from_v2(created.topology),
        )
    elif payload == "run_status_changed":
        summary = msg.run_status_changed.summary
        change = RunStatusChanged(
            run_id=summary.run_id,
            status=RunStatus(summary.status),
            started_at=summary.started_at or None,
            ended_at=summary.ended_at or None,
            revision=summary.revision,
        )
    elif payload == "node_status_changed":
        changed = msg.node_status_changed
        node = changed.node
        change = NodeStatusChanged(
            run_id=changed.run_id,
            node_id=node.node_id,
            status=NodeStatus(node.status),
            started_at=node.started_at or None,
            ended_at=node.ended_at or None,
            error=node.error if node.HasField("error") else None,
            revision=node.revision,
            running_elapsed_seconds=(
                node.running_elapsed_seconds
                if node.HasField("running_elapsed_seconds")
                else None
            ),
        )
    elif payload == "activity_appended":
        appended = msg.activity_appended
        activity = appended.activity
        if activity.kind == "log":
            change = LogAppended(
                run_id=appended.run_id,
                log=log_record_descriptor_from_v2(activity),
            )
        elif activity.kind == "agent_event":
            change = AgentEventAppended(
                run_id=appended.run_id,
                node_id=activity.node_id,
                event=agent_event_descriptor_from_v2(activity),
            )
        elif activity.kind == "trace":
            change = TraceFinalized(
                run_id=appended.run_id,
                node_id=activity.node_id,
                trace=trace_descriptor_from_v2(activity.trace),
            )
        else:
            raise ValueError(f"Unknown activity kind: {activity.kind}")
    elif payload == "flow_list_changed":
        change = CatalogReplaced(
            catalog=catalog_snapshot_from_v2(msg.flow_list_changed.flow_list)
        )
    elif payload == "flow_reload_status":
        change = WorkflowReloadStatus(reloading=msg.flow_reload_status.reloading)
    elif payload is None:
        raise ValueError("run status envelope requires a payload")
    else:
        raise ValueError(f"Unknown run status payload: {payload}")
    return OperatorUpdateEnvelope(
        operator_instance_id=instance,
        update=OperatorUpdate(sequence=0, event_ulid=event_ulid, change=change),
    )
