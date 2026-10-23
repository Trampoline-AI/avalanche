"""Shared state models and UI-owned bounded presentation data."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from runtime.operator.models import (
    AgentEventDetailAppended,
    CatalogSnapshot,
    DetailUpdate,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    ResetBaseline,
    RunState,
    RunStatus,
    StreamResetNotice,
    TraceDescriptor,
    TraceDetail,
    WorkflowInfo,
    display_name_from_id,
)

LOG_WINDOW_RECORDS = 10_000
LOG_WINDOW_BYTES = 8 * 1024 * 1024
LOG_MESSAGE_BYTES = 8192


@dataclass(frozen=True)
class StreamLogEntry(LogEntry):
    """Optional activity metadata; None means the provider does not expose it."""

    channel: str | None = None
    attempt: str | None = None


class LogWindow(list[LogEntry]):
    """A UI-owned tail. Stream watermarks remain absolute when old rows leave.

    The byte ceiling includes encoded text and a conservative per-record object
    allowance. Oversized individual records carry an explicit truncation marker.
    Never mutate a provider's list to enforce presentation retention.
    """

    def __init__(self, entries: Iterable[LogEntry] = ()) -> None:
        super().__init__()
        self.evicted = 0
        self.retained_bytes = 0
        self.revision = 0
        if isinstance(entries, Sequence) and len(entries) > LOG_WINDOW_RECORDS:
            self.evicted = len(entries) - LOG_WINDOW_RECORDS
            entries = entries[-LOG_WINDOW_RECORDS:]
        self.extend(entries)
        if isinstance(entries, LogWindow):
            self.evicted += entries.evicted

    @staticmethod
    def entry_bytes(entry: LogEntry) -> int:
        return 512 + len(entry.message.encode("utf-8")) + len(entry.node_id.encode("utf-8"))

    def append(self, entry: LogEntry) -> None:
        raw = entry.message.encode("utf-8")
        if len(raw) > LOG_MESSAGE_BYTES:
            entry = replace(
                entry,
                message=raw[:LOG_MESSAGE_BYTES].decode("utf-8", "ignore")
                + " … [record truncated]",
            )
        super().append(entry)
        self.retained_bytes += self.entry_bytes(entry)
        while len(self) > LOG_WINDOW_RECORDS or self.retained_bytes > LOG_WINDOW_BYTES:
            self.retained_bytes -= self.entry_bytes(self[0])
            del self[0]
            self.evicted += 1
        self.revision += 1

    def extend(self, entries: Iterable[LogEntry]) -> None:
        for entry in entries:
            self.append(entry)


def bound_run(run: RunState) -> RunState:
    """Detach UI retention from provider bodies without changing execution truth."""
    budget = 8 * 1024 * 1024
    nodes = None
    for node_id, node in run.nodes.items():
        if node.agent_trace_json is None:
            continue
        size = len(node.agent_trace_json.encode("utf-8"))
        if size <= budget:
            budget -= size
            continue
        if nodes is None:
            nodes = dict(run.nodes)
        descriptor = node.trace
        nodes[node_id] = replace(
            node,
            agent_trace_json=None,
            trace=replace(descriptor, status="ui_truncated", available=False, complete=False)
            if descriptor is not None
            else TraceDescriptor(status="ui_truncated", size_bytes=size),
        )
    return replace(
        run, logs=LogWindow(run.logs), nodes=nodes if nodes is not None else run.nodes
    )


__all__ = [
    "AgentEventDetailAppended",
    "CatalogSnapshot",
    "DetailUpdate",
    "LogEntry",
    "LogLevel",
    "LogDetailAppended",
    "NodeState",
    "ResetBaseline",
    "NodeStatus",
    "WorkflowInfo",
    "RunState",
    "TraceDetail",
    "RunStatus",
    "StreamResetNotice",
    "display_name_from_id",
]
