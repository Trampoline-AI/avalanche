"""Shared operator/TUI state models with no TUI dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class NodeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass
class LogEntry:
    timestamp: datetime
    level: LogLevel
    node_id: str
    message: str


@dataclass
class NodeState:
    node_id: str
    name: str
    node_type: str  # "source" | "step" | "dest"
    status: NodeStatus = NodeStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return end - self.started_at


@dataclass
class RunState:
    run_id: str
    flow_name: str
    status: RunStatus = RunStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    logs: list[LogEntry] = field(default_factory=list)
    triggered_by: str = "manual"  # "manual" | "scheduled"

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return end - self.started_at


@dataclass
class WorkflowInfo:
    """Flat snapshot of a workflow — the TUI never holds real Workflow objects."""

    name: str
    file_path: str
    node_ids: list[str]  # topological order (future_ids, e.g. "fetch_orders_1")
    graph: dict[str, list[str]]  # adjacency list (parent -> children)
    node_types: dict[str, str]  # node_id -> "source" | "step" | "dest"
    display_names: dict[str, str] = field(default_factory=dict)  # node_id -> display name
    cron: str | None = None  # cron expression for scheduled execution
    next_run_at: float | None = None  # unix timestamp of next scheduled run
    last_run_at: float | None = None  # unix timestamp of last triggered run


@dataclass(frozen=True)
class WorkflowDiscoveryDiagnostic:
    path: str
    kind: Literal["skipped", "import_error"]
    message: str


def display_name_from_id(node_id: str) -> str:
    """Derive display name from a future_id: 'fetch_orders_1' → 'fetch_orders'."""
    parts = node_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return node_id
