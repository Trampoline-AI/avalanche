"""Conversion between Python models and protobuf messages."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from .models import (
    AgentEvent,
    AgentEventAppended,
    LogAppended,
    LogEntry,
    LogLevel,
    NodeSnapshot,
    NodeState,
    NodeStatus,
    NodeStatusChanged,
    ResetRequired,
    RunCreated,
    RunDelta,
    RunDeltaEnvelope,
    RunSnapshot,
    RunState,
    RunStatus,
    RunStatusChanged,
    RunSummary,
    SequencedLogEntry,
    TraceDescriptor,
    TraceFinalized,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
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


def node_state_to_proto(ns: NodeState) -> pb.NodeStateMsg:
    return pb.NodeStateMsg(
        node_id=ns.node_id,
        name=ns.name,
        node_type=ns.node_type,
        status=ns.status.value,
        started_at=ns.started_at or 0.0,
        ended_at=ns.ended_at or 0.0,
        agent_trace_json=ns.agent_trace_json or "",
    )


def node_state_from_proto(msg: pb.NodeStateMsg) -> NodeState:
    return NodeState(
        node_id=msg.node_id,
        name=msg.name,
        node_type=msg.node_type,
        status=NodeStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        agent_trace_json=msg.agent_trace_json or None,
    )


def run_state_to_proto(run: RunState) -> pb.RunStateMsg:
    return pb.RunStateMsg(
        run_id=run.run_id,
        flow_name=run.flow_name,
        status=run.status.value,
        started_at=run.started_at or 0.0,
        ended_at=run.ended_at or 0.0,
        nodes=[node_state_to_proto(ns) for ns in run.nodes.values()],
        logs=[log_entry_to_proto(le) for le in run.logs],
        triggered_by=run.triggered_by,
        workflow_id=run.workflow_id or run.flow_name,
        workflow_display_name=run.workflow_display_name or run.flow_name,
    )


def run_state_from_proto(msg: pb.RunStateMsg) -> RunState:
    run = RunState(
        run_id=msg.run_id,
        flow_name=msg.flow_name,
        status=RunStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        triggered_by=msg.triggered_by or "manual",
        workflow_id=msg.workflow_id or msg.flow_name,
        workflow_display_name=msg.workflow_display_name or msg.flow_name,
    )
    for ns_msg in msg.nodes:
        ns = node_state_from_proto(ns_msg)
        run.nodes[ns.node_id] = ns
    for le_msg in msg.logs:
        run.logs.append(log_entry_from_proto(le_msg))
    return run


def log_entry_to_proto(le: LogEntry) -> pb.LogEntryMsg:
    return pb.LogEntryMsg(
        timestamp=le.timestamp.timestamp(),
        level=le.level.value,
        node_id=le.node_id,
        message=le.message,
    )


def log_entry_from_proto(msg: pb.LogEntryMsg) -> LogEntry:
    from datetime import datetime

    return LogEntry(
        timestamp=datetime.fromtimestamp(msg.timestamp),
        level=LogLevel(msg.level),
        node_id=msg.node_id,
        message=msg.message,
    )


def trace_descriptor_to_proto(descriptor: TraceDescriptor) -> pb.TraceDescriptorMsg:
    return pb.TraceDescriptorMsg(
        status=descriptor.status,
        revision=descriptor.revision,
        available=descriptor.available,
        complete=descriptor.complete,
        event_count=descriptor.event_count,
        size_bytes=descriptor.size_bytes,
    )


def trace_descriptor_from_proto(msg: pb.TraceDescriptorMsg) -> TraceDescriptor:
    return TraceDescriptor(
        status=msg.status,
        revision=msg.revision,
        available=msg.available,
        complete=msg.complete,
        event_count=msg.event_count,
        size_bytes=msg.size_bytes,
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
    return message


def node_snapshot_from_proto(msg: pb.NodeSnapshotMsg) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=msg.node_id,
        name=msg.name,
        node_type=msg.node_type,
        status=NodeStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        trace=trace_descriptor_from_proto(msg.trace) if msg.HasField("trace") else None,
        revision=msg.revision,
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
    )


def run_snapshot_from_proto(msg: pb.RunSnapshotMsg) -> RunSnapshot:
    return RunSnapshot(
        operator_instance_id=msg.operator_instance_id,
        as_of_sequence=msg.as_of_sequence,
        summary=run_summary_from_proto(msg.summary),
        nodes=tuple(node_snapshot_from_proto(node) for node in msg.nodes),
        latest_log_sequence=msg.latest_log_sequence,
    )


def sequenced_log_entry_to_proto(log: SequencedLogEntry) -> pb.SequencedLogEntryMsg:
    return pb.SequencedLogEntryMsg(
        sequence=log.sequence,
        entry=log_entry_to_proto(log.entry),
    )


def sequenced_log_entry_from_proto(msg: pb.SequencedLogEntryMsg) -> SequencedLogEntry:
    return SequencedLogEntry(
        sequence=msg.sequence,
        entry=log_entry_from_proto(msg.entry),
    )


def agent_event_to_proto(event: AgentEvent) -> pb.AgentEventMsg:
    return pb.AgentEventMsg(
        event_sequence=event.event_sequence,
        event_json=event.event_json,
    )


def agent_event_from_proto(msg: pb.AgentEventMsg) -> AgentEvent:
    return AgentEvent(
        event_sequence=msg.event_sequence,
        event_json=msg.event_json,
    )


def run_delta_to_proto(delta: RunDelta) -> pb.RunDelta:
    message = pb.RunDelta(sequence=delta.sequence)
    change = delta.change
    if isinstance(change, RunCreated):
        message.run_created.CopyFrom(
            pb.RunCreatedDelta(
                summary=run_summary_to_proto(change.summary),
                nodes=[node_snapshot_to_proto(node) for node in change.nodes],
            )
        )
    elif isinstance(change, RunStatusChanged):
        message.run_status_changed.CopyFrom(
            pb.RunStatusChangedDelta(
                run_id=change.run_id,
                status=change.status.value,
                started_at=change.started_at or 0.0,
                ended_at=change.ended_at or 0.0,
                revision=change.revision,
            )
        )
    elif isinstance(change, NodeStatusChanged):
        message.node_status_changed.CopyFrom(
            pb.NodeStatusChangedDelta(
                run_id=change.run_id,
                node_id=change.node_id,
                status=change.status.value,
                started_at=change.started_at or 0.0,
                ended_at=change.ended_at or 0.0,
                revision=change.revision,
            )
        )
    elif isinstance(change, LogAppended):
        message.log_appended.CopyFrom(
            pb.LogAppendedDelta(
                run_id=change.run_id,
                log=sequenced_log_entry_to_proto(change.log),
            )
        )
    elif isinstance(change, AgentEventAppended):
        message.agent_event_appended.CopyFrom(
            pb.AgentEventAppendedDelta(
                run_id=change.run_id,
                node_id=change.node_id,
                event=agent_event_to_proto(change.event),
            )
        )
    elif isinstance(change, TraceFinalized):
        message.trace_finalized.CopyFrom(
            pb.TraceFinalizedDelta(
                run_id=change.run_id,
                node_id=change.node_id,
                trace=trace_descriptor_to_proto(change.trace),
            )
        )
    else:
        raise TypeError(f"Unsupported run delta change: {type(change).__name__}")
    return message


def run_delta_from_proto(msg: pb.RunDelta) -> RunDelta:
    change_name = msg.WhichOneof("change")
    if change_name == "run_created":
        change = RunCreated(
            summary=run_summary_from_proto(msg.run_created.summary),
            nodes=tuple(node_snapshot_from_proto(node) for node in msg.run_created.nodes),
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
            revision=item.revision,
        )
    elif change_name == "log_appended":
        item = msg.log_appended
        change = LogAppended(
            run_id=item.run_id,
            log=sequenced_log_entry_from_proto(item.log),
        )
    elif change_name == "agent_event_appended":
        item = msg.agent_event_appended
        change = AgentEventAppended(
            run_id=item.run_id,
            node_id=item.node_id,
            event=agent_event_from_proto(item.event),
        )
    elif change_name == "trace_finalized":
        item = msg.trace_finalized
        change = TraceFinalized(
            run_id=item.run_id,
            node_id=item.node_id,
            trace=trace_descriptor_from_proto(item.trace),
        )
    else:
        raise ValueError("run delta is missing a change")
    return RunDelta(sequence=msg.sequence, change=change)


def run_delta_envelope_to_proto(envelope: RunDeltaEnvelope) -> pb.RunDeltaEnvelope:
    message = pb.RunDeltaEnvelope(operator_instance_id=envelope.operator_instance_id)
    if envelope.delta is not None:
        message.delta.CopyFrom(run_delta_to_proto(envelope.delta))
    elif envelope.reset_required is not None:
        message.reset_required.CopyFrom(
            pb.ResetRequired(
                history_floor=envelope.reset_required.history_floor,
                latest_sequence=envelope.reset_required.latest_sequence,
            )
        )
    return message


def run_delta_envelope_from_proto(msg: pb.RunDeltaEnvelope) -> RunDeltaEnvelope:
    payload = msg.WhichOneof("payload")
    if payload == "delta":
        return RunDeltaEnvelope(
            operator_instance_id=msg.operator_instance_id,
            delta=run_delta_from_proto(msg.delta),
        )
    if payload == "reset_required":
        return RunDeltaEnvelope(
            operator_instance_id=msg.operator_instance_id,
            reset_required=ResetRequired(
                history_floor=msg.reset_required.history_floor,
                latest_sequence=msg.reset_required.latest_sequence,
            ),
        )
    raise ValueError("run delta envelope is missing a payload")


def _relative_source_file(info: WorkflowInfo) -> str:
    """Return a stable relative source path for the public wire model."""
    raw = info.relative_file or info.file_path
    if not raw:
        return ""
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        return PureWindowsPath(raw).name if "\\" in raw else Path(raw).name
    return raw.replace("\\", "/")
