"""Conversion between Python models and protobuf messages."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from .models import (
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
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
        node_slugs=info.node_slugs,
        cron=info.cron or "",
        next_run_at=info.next_run_at or 0.0,
        last_run_at=info.last_run_at or 0.0,
        workflow_id=info.workflow_id or info.name,
        display_name=display_name,
        root_alias=info.root_alias,
        relative_file=relative_file,
        builder_symbol=info.builder_symbol,
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
        node_slugs=dict(msg.node_slugs),
        cron=msg.cron if msg.cron else None,
        next_run_at=msg.next_run_at if msg.next_run_at else None,
        last_run_at=msg.last_run_at if msg.last_run_at else None,
        workflow_id=msg.workflow_id or msg.name,
        display_name=display_name,
        root_alias=msg.root_alias,
        relative_file=relative_file,
        builder_symbol=msg.builder_symbol,
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
    )


def node_state_from_proto(msg: pb.NodeStateMsg) -> NodeState:
    return NodeState(
        node_id=msg.node_id,
        name=msg.name,
        node_type=msg.node_type,
        status=NodeStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
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
        rerun_of=run.rerun_of or "",
        rerun_start=run.rerun_start,
        rerun_mode=run.rerun_mode or "",
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
        rerun_of=msg.rerun_of or None,
        rerun_start=tuple(msg.rerun_start),
        rerun_mode=msg.rerun_mode or None,
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


def _relative_source_file(info: WorkflowInfo) -> str:
    """Return a stable relative source path for the public wire model."""
    raw = info.relative_file or info.file_path
    if not raw:
        return ""
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        return PureWindowsPath(raw).name if "\\" in raw else Path(raw).name
    return raw.replace("\\", "/")
