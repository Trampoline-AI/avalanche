"""Conversion between Python models and protobuf messages."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

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
    WorkflowTopology,
)
from .proto import operator_pb2 as pb


def workflow_info_to_proto(info: WorkflowInfo) -> pb.FlowInfoMsg:
    graph = {}
    for parent, children in info.graph.items():
        graph[parent] = pb.NodeEdges(children=children)
    relative_file = _relative_source_file(info)
    display_name = info.display_name or info.name
    return pb.FlowInfoMsg(
        name=info.name or display_name,
        file_path=relative_file,
        node_ids=info.node_ids,
        graph=graph,
        node_types=info.node_types,
        display_names=info.display_names,
        cron=info.cron or "",
        next_run_at=info.next_run_at or 0.0,
        last_run_at=info.last_run_at or 0.0,
        workflow_id=info.workflow_id or info.name,
        display_name=display_name,
        root_alias=info.root_alias,
        relative_file=relative_file,
        builder_symbol=info.builder_symbol,
        agent_node_ids=info.agent_node_ids,
        agent_metadata_json=info.agent_metadata_json,
        webhook_path=info.webhook_path or "",
        webhook_url=info.webhook_url or "",
        webhook_active=info.webhook_active,
    )


def workflow_info_from_proto(msg: pb.FlowInfoMsg) -> WorkflowInfo:
    graph = {parent: list(edges.children) for parent, edges in msg.graph.items()}
    display_name = msg.display_name or msg.name
    relative_file = msg.relative_file or msg.file_path
    return WorkflowInfo(
        name=msg.name or display_name,
        file_path=relative_file,
        node_ids=list(msg.node_ids),
        graph=graph,
        node_types=dict(msg.node_types),
        display_names=dict(msg.display_names),
        agent_node_ids=list(msg.agent_node_ids),
        agent_metadata_json=dict(msg.agent_metadata_json),
        cron=msg.cron if msg.cron else None,
        next_run_at=msg.next_run_at if msg.next_run_at else None,
        last_run_at=msg.last_run_at if msg.last_run_at else None,
        workflow_id=msg.workflow_id or msg.name,
        display_name=display_name,
        root_alias=msg.root_alias,
        relative_file=relative_file,
        builder_symbol=msg.builder_symbol,
        webhook_path=msg.webhook_path or None,
        webhook_url=msg.webhook_url or None,
        webhook_active=msg.webhook_active,
    )


def discovery_diagnostic_to_proto(
    diagnostic: WorkflowDiscoveryDiagnostic,
) -> pb.DiscoveryDiagnosticMsg:
    return pb.DiscoveryDiagnosticMsg(
        path=diagnostic.path,
        kind=diagnostic.kind,
        message=diagnostic.message,
    )


def discovery_diagnostic_from_proto(
    msg: pb.DiscoveryDiagnosticMsg,
) -> WorkflowDiscoveryDiagnostic:
    return WorkflowDiscoveryDiagnostic(
        path=msg.path,
        kind=msg.kind,
        message=msg.message,
    )


def scan_target_to_proto(target: ScanTargetInfo) -> pb.ScanTargetMsg:
    return pb.ScanTargetMsg(
        alias=target.alias,
        target_path=target.target_path,
        kind=target.kind,
    )


def scan_target_from_proto(msg: pb.ScanTargetMsg) -> ScanTargetInfo:
    if msg.kind not in {"file", "directory"}:
        raise ValueError(f"Unknown scan target kind: {msg.kind}")
    return ScanTargetInfo(
        alias=msg.alias,
        target_path=msg.target_path,
        kind=msg.kind,
    )


def catalog_snapshot_to_proto(catalog: CatalogSnapshot) -> pb.CatalogSnapshotMsg:
    return pb.CatalogSnapshotMsg(
        revision=catalog.revision,
        operator_instance_id=catalog.operator_instance_id,
        as_of_sequence=catalog.as_of_sequence,
        workflows=[workflow_info_to_proto(item) for item in catalog.workflows],
        scan_targets=[scan_target_to_proto(item) for item in catalog.scan_targets],
        diagnostics=[discovery_diagnostic_to_proto(item) for item in catalog.diagnostics],
    )


def catalog_snapshot_from_proto(msg: pb.CatalogSnapshotMsg) -> CatalogSnapshot:
    return CatalogSnapshot(
        revision=msg.revision,
        workflows=tuple(workflow_info_from_proto(item) for item in msg.workflows),
        operator_instance_id=msg.operator_instance_id,
        as_of_sequence=msg.as_of_sequence,
        scan_targets=tuple(scan_target_from_proto(item) for item in msg.scan_targets),
        diagnostics=tuple(discovery_diagnostic_from_proto(item) for item in msg.diagnostics),
    )


def workflow_topology_to_proto(topology: WorkflowTopology) -> pb.WorkflowTopologyMsg:
    return pb.WorkflowTopologyMsg(
        node_ids=topology.node_ids,
        graph={parent: pb.NodeEdges(children=children) for parent, children in topology.graph},
        node_types=dict(topology.node_types),
        display_names=dict(topology.display_names),
        agent_metadata_json=dict(topology.agent_metadata_json),
    )


def workflow_topology_from_proto(msg: pb.WorkflowTopologyMsg) -> WorkflowTopology:
    node_ids = tuple(msg.node_ids)
    return WorkflowTopology(
        node_ids=node_ids,
        graph=tuple((node_id, tuple(msg.graph[node_id].children)) for node_id in node_ids),
        node_types=tuple((node_id, msg.node_types[node_id]) for node_id in node_ids),
        display_names=tuple((node_id, msg.display_names[node_id]) for node_id in node_ids),
        agent_metadata_json=tuple(
            (node_id, msg.agent_metadata_json[node_id])
            for node_id in node_ids
            if node_id in msg.agent_metadata_json
        ),
    )


def trace_header_to_proto(header: TraceHeader) -> pb.TraceHeaderMsg:
    message = pb.TraceHeaderMsg(
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


def trace_header_from_proto(msg: pb.TraceHeaderMsg) -> TraceHeader:
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


def trace_descriptor_to_proto(descriptor: TraceDescriptor) -> pb.TraceDescriptorMsg:
    message = pb.TraceDescriptorMsg(
        status=descriptor.status,
        revision=descriptor.revision,
        available=descriptor.available,
        complete=descriptor.complete,
        event_count=descriptor.event_count,
        size_bytes=descriptor.size_bytes,
        latest_event_sequence=descriptor.latest_event_sequence,
    )
    if descriptor.header is not None:
        message.header.CopyFrom(trace_header_to_proto(descriptor.header))
    return message


def trace_descriptor_from_proto(msg: pb.TraceDescriptorMsg) -> TraceDescriptor:
    return TraceDescriptor(
        status=msg.status,
        revision=msg.revision,
        available=msg.available,
        complete=msg.complete,
        event_count=msg.event_count,
        size_bytes=msg.size_bytes,
        latest_event_sequence=msg.latest_event_sequence,
        header=trace_header_from_proto(msg.header) if msg.HasField("header") else None,
    )


def node_snapshot_to_proto(node: NodeSnapshot) -> pb.NodeSnapshotMsg:
    message = pb.NodeSnapshotMsg(
        node_id=node.node_id,
        name=node.name,
        node_type=node.node_type,
        status=node.status.value,
        started_at=node.started_at or 0.0,
        ended_at=node.ended_at or 0.0,
        revision=node.revision,
    )
    if node.trace is not None:
        message.trace.CopyFrom(trace_descriptor_to_proto(node.trace))
    message.event_page_token = node.event_page_token
    if node.error is not None:
        message.error = node.error
    return message


def node_snapshot_from_proto(msg: pb.NodeSnapshotMsg) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=msg.node_id,
        name=msg.name,
        node_type=msg.node_type,
        status=NodeStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        error=msg.error if msg.HasField("error") else None,
        trace=trace_descriptor_from_proto(msg.trace) if msg.HasField("trace") else None,
        revision=msg.revision,
        event_page_token=msg.event_page_token,
    )


def run_summary_to_proto(summary: RunSummary) -> pb.RunSummaryMsg:
    return pb.RunSummaryMsg(
        run_id=summary.run_id,
        flow_name=summary.flow_name,
        status=summary.status.value,
        started_at=summary.started_at or 0.0,
        ended_at=summary.ended_at or 0.0,
        triggered_by=summary.triggered_by,
        workflow_id=summary.workflow_id or summary.flow_name,
        workflow_display_name=summary.workflow_display_name or summary.flow_name,
        created_sequence=summary.created_sequence,
        revision=summary.revision,
    )


def run_summary_from_proto(msg: pb.RunSummaryMsg) -> RunSummary:
    return RunSummary(
        run_id=msg.run_id,
        flow_name=msg.flow_name,
        status=RunStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        triggered_by=msg.triggered_by or "manual",
        workflow_id=msg.workflow_id or msg.flow_name,
        workflow_display_name=msg.workflow_display_name or msg.flow_name,
        created_sequence=msg.created_sequence,
        revision=msg.revision,
    )


def run_snapshot_to_proto(snapshot: RunSnapshot) -> pb.RunSnapshotMsg:
    return pb.RunSnapshotMsg(
        operator_instance_id=snapshot.operator_instance_id,
        as_of_sequence=snapshot.as_of_sequence,
        summary=run_summary_to_proto(snapshot.summary),
        nodes=[node_snapshot_to_proto(node) for node in snapshot.nodes],
        latest_log_sequence=snapshot.latest_log_sequence,
        log_page_token=snapshot.log_page_token,
        topology=workflow_topology_to_proto(snapshot.topology),
    )


def run_snapshot_from_proto(msg: pb.RunSnapshotMsg) -> RunSnapshot:
    return RunSnapshot(
        operator_instance_id=msg.operator_instance_id,
        as_of_sequence=msg.as_of_sequence,
        summary=run_summary_from_proto(msg.summary),
        nodes=tuple(node_snapshot_from_proto(node) for node in msg.nodes),
        latest_log_sequence=msg.latest_log_sequence,
        log_page_token=msg.log_page_token,
        topology=workflow_topology_from_proto(msg.topology),
    )


def log_record_descriptor_to_proto(
    log: LogRecordDescriptor,
) -> pb.LogRecordDescriptorMsg:
    return pb.LogRecordDescriptorMsg(
        sequence=log.sequence,
        timestamp=log.timestamp.timestamp(),
        level=log.level.value,
        node_id=log.node_id,
        size_bytes=log.size_bytes,
        body_token=log.body_token,
    )


def log_record_descriptor_from_proto(
    msg: pb.LogRecordDescriptorMsg,
) -> LogRecordDescriptor:
    from datetime import datetime

    return LogRecordDescriptor(
        sequence=msg.sequence,
        timestamp=datetime.fromtimestamp(msg.timestamp),
        level=LogLevel(msg.level),
        node_id=msg.node_id,
        size_bytes=msg.size_bytes,
        body_token=msg.body_token,
    )


def agent_event_descriptor_to_proto(
    event: AgentEventDescriptor,
) -> pb.AgentEventDescriptorMsg:
    message = pb.AgentEventDescriptorMsg(
        invocation_id=event.invocation_id,
        event_sequence=event.event_sequence,
        size_bytes=event.size_bytes,
        body_token=event.body_token,
        event_kind=event.event_kind,
        error=event.error,
        tool_count=event.tool_count,
        predict_count=event.predict_count,
    )
    if event.iteration is not None:
        message.iteration = event.iteration
    if event.duration_ms is not None:
        message.duration_ms = event.duration_ms
    return message


def agent_event_descriptor_from_proto(
    msg: pb.AgentEventDescriptorMsg,
) -> AgentEventDescriptor:
    return AgentEventDescriptor(
        invocation_id=msg.invocation_id,
        event_sequence=msg.event_sequence,
        size_bytes=msg.size_bytes,
        body_token=msg.body_token,
        event_kind=msg.event_kind,
        iteration=msg.iteration if msg.HasField("iteration") else None,
        duration_ms=msg.duration_ms if msg.HasField("duration_ms") else None,
        error=msg.error,
        tool_count=msg.tool_count,
        predict_count=msg.predict_count,
    )


def operator_update_to_proto(update: OperatorUpdate) -> pb.OperatorUpdate:
    message = pb.OperatorUpdate(sequence=update.sequence)
    change = update.change
    if isinstance(change, RunCreated):
        message.run_created.CopyFrom(
            pb.RunCreated(
                summary=run_summary_to_proto(change.summary),
                nodes=[node_snapshot_to_proto(node) for node in change.nodes],
                topology=workflow_topology_to_proto(change.topology),
            )
        )
    elif isinstance(change, RunStatusChanged):
        message.run_status_changed.CopyFrom(
            pb.RunStatusChanged(
                run_id=change.run_id,
                status=change.status.value,
                started_at=change.started_at or 0.0,
                ended_at=change.ended_at or 0.0,
                revision=change.revision,
            )
        )
    elif isinstance(change, NodeStatusChanged):
        changed = pb.NodeStatusChanged(
            run_id=change.run_id,
            node_id=change.node_id,
            status=change.status.value,
            started_at=change.started_at or 0.0,
            ended_at=change.ended_at or 0.0,
            revision=change.revision,
        )
        if change.error is not None:
            changed.error = change.error
        message.node_status_changed.CopyFrom(changed)
    elif isinstance(change, LogAppended):
        message.log_appended.CopyFrom(
            pb.LogAppended(
                run_id=change.run_id,
                log=log_record_descriptor_to_proto(change.log),
            )
        )
    elif isinstance(change, AgentEventAppended):
        message.agent_event_appended.CopyFrom(
            pb.AgentEventAppended(
                run_id=change.run_id,
                node_id=change.node_id,
                event=agent_event_descriptor_to_proto(change.event),
            )
        )
    elif isinstance(change, TraceFinalized):
        message.trace_finalized.CopyFrom(
            pb.TraceFinalized(
                run_id=change.run_id,
                node_id=change.node_id,
                trace=trace_descriptor_to_proto(change.trace),
            )
        )
    elif isinstance(change, CatalogReplaced):
        message.catalog_replaced.CopyFrom(
            pb.CatalogReplaced(catalog=catalog_snapshot_to_proto(change.catalog))
        )
    else:
        raise TypeError(f"Unsupported operator update change: {type(change).__name__}")
    return message


def operator_update_from_proto(msg: pb.OperatorUpdate) -> OperatorUpdate:
    change_name = msg.WhichOneof("change")
    if change_name == "run_created":
        change = RunCreated(
            summary=run_summary_from_proto(msg.run_created.summary),
            nodes=tuple(node_snapshot_from_proto(node) for node in msg.run_created.nodes),
            topology=workflow_topology_from_proto(msg.run_created.topology),
        )
    elif change_name == "run_status_changed":
        item = msg.run_status_changed
        change = RunStatusChanged(
            run_id=item.run_id,
            status=RunStatus(item.status),
            started_at=item.started_at if item.started_at else None,
            ended_at=item.ended_at if item.ended_at else None,
            revision=item.revision,
        )
    elif change_name == "node_status_changed":
        item = msg.node_status_changed
        change = NodeStatusChanged(
            run_id=item.run_id,
            node_id=item.node_id,
            status=NodeStatus(item.status),
            started_at=item.started_at if item.started_at else None,
            ended_at=item.ended_at if item.ended_at else None,
            error=item.error if item.HasField("error") else None,
            revision=item.revision,
        )
    elif change_name == "log_appended":
        item = msg.log_appended
        change = LogAppended(
            run_id=item.run_id,
            log=log_record_descriptor_from_proto(item.log),
        )
    elif change_name == "agent_event_appended":
        item = msg.agent_event_appended
        change = AgentEventAppended(
            run_id=item.run_id,
            node_id=item.node_id,
            event=agent_event_descriptor_from_proto(item.event),
        )
    elif change_name == "trace_finalized":
        item = msg.trace_finalized
        change = TraceFinalized(
            run_id=item.run_id,
            node_id=item.node_id,
            trace=trace_descriptor_from_proto(item.trace),
        )
    elif change_name == "catalog_replaced":
        change = CatalogReplaced(
            catalog=catalog_snapshot_from_proto(msg.catalog_replaced.catalog)
        )
    else:
        raise ValueError("operator update is missing a change")
    return OperatorUpdate(sequence=msg.sequence, change=change)


def operator_update_envelope_to_proto(
    envelope: OperatorUpdateEnvelope,
) -> pb.OperatorUpdateEnvelope:
    message = pb.OperatorUpdateEnvelope(operator_instance_id=envelope.operator_instance_id)
    if envelope.update is not None:
        message.update.CopyFrom(operator_update_to_proto(envelope.update))
    elif envelope.reset_required is not None:
        message.reset_required.CopyFrom(
            pb.ResetRequired(
                history_floor=envelope.reset_required.history_floor,
                latest_sequence=envelope.reset_required.latest_sequence,
            )
        )
    return message


def operator_update_envelope_from_proto(
    msg: pb.OperatorUpdateEnvelope,
) -> OperatorUpdateEnvelope:
    payload = msg.WhichOneof("payload")
    if payload == "update":
        return OperatorUpdateEnvelope(
            operator_instance_id=msg.operator_instance_id,
            update=operator_update_from_proto(msg.update),
        )
    if payload == "reset_required":
        return OperatorUpdateEnvelope(
            operator_instance_id=msg.operator_instance_id,
            reset_required=ResetRequired(
                history_floor=msg.reset_required.history_floor,
                latest_sequence=msg.reset_required.latest_sequence,
            ),
        )
    raise ValueError("operator update envelope is missing a payload")


def _relative_source_file(info: WorkflowInfo) -> str:
    """Return a stable relative source path for the public wire model."""
    raw = info.relative_file or info.file_path
    if not raw:
        return ""
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        return PureWindowsPath(raw).name if "\\" in raw else Path(raw).name
    return raw.replace("\\", "/")
