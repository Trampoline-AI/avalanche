"""Conversion between Python models and protobuf messages."""

from __future__ import annotations

from .models import (
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    RunState,
    RunStatus,
    WorkflowInfo,
)
from .proto import operator_pb2 as pb


def workflow_info_to_proto(info: WorkflowInfo) -> pb.FlowInfoMsg:
    graph = {}
    for parent, children in info.graph.items():
        graph[parent] = pb.NodeEdges(children=children)
    return pb.FlowInfoMsg(
        name=info.name,
        file_path=info.file_path,
        node_ids=info.node_ids,
        graph=graph,
        node_types=info.node_types,
        display_names=info.display_names,
        cron=info.cron or "",
        next_run_at=info.next_run_at or 0.0,
        last_run_at=info.last_run_at or 0.0,
    )


def workflow_info_from_proto(msg: pb.FlowInfoMsg) -> WorkflowInfo:
    graph = {parent: list(edges.children) for parent, edges in msg.graph.items()}
    return WorkflowInfo(
        name=msg.name,
        file_path=msg.file_path,
        node_ids=list(msg.node_ids),
        graph=graph,
        node_types=dict(msg.node_types),
        display_names=dict(msg.display_names),
        cron=msg.cron if msg.cron else None,
        next_run_at=msg.next_run_at if msg.next_run_at else None,
        last_run_at=msg.last_run_at if msg.last_run_at else None,
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
    )


def run_state_from_proto(msg: pb.RunStateMsg) -> RunState:
    run = RunState(
        run_id=msg.run_id,
        flow_name=msg.flow_name,
        status=RunStatus(msg.status),
        started_at=msg.started_at if msg.started_at else None,
        ended_at=msg.ended_at if msg.ended_at else None,
        triggered_by=msg.triggered_by or "manual",
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
