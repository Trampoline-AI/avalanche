"""Shared operator/TUI state models with no TUI dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias


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


RerunMode: TypeAlias = Literal["autorun", "lazy"]


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
    workflow_id: str = ""
    workflow_display_name: str = ""
    rerun_of: str | None = None
    rerun_start: tuple[str, ...] = ()
    rerun_mode: RerunMode | None = None

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
    node_slugs: dict[str, str] = field(default_factory=dict)  # node_id -> stable slug
    cron: str | None = None  # cron expression for scheduled execution
    next_run_at: float | None = None  # unix timestamp of next scheduled run
    last_run_at: float | None = None  # unix timestamp of last triggered run
    workflow_id: str = ""
    display_name: str = ""
    builder_symbol: str = ""
    root_alias: str = ""
    relative_file: str = ""

    @property
    def selector(self) -> str:
        """Canonical selector, falling back for pre-identity fixtures."""
        return self.workflow_id or self.name

    @property
    def rendered_name(self) -> str:
        """Human-readable name, falling back for pre-identity fixtures."""
        return self.display_name or self.name

    @property
    def source_file(self) -> str:
        """Relative source path, falling back for pre-identity fixtures."""
        return self.relative_file or self.file_path


@dataclass(frozen=True)
class WorkflowDiscoveryDiagnostic:
    path: str
    kind: Literal["skipped", "import_error", "build_error", "invalid_schedule"]
    message: str


@dataclass(frozen=True)
class WorkflowLocator:
    """Stable source identity without an executable or absolute path."""

    root_alias: str
    relative_file: str
    builder_symbol: str


@dataclass(frozen=True)
class WorkflowDescriptor:
    """Immutable, serializable metadata produced by discovery."""

    workflow_id: str
    display_name: str
    locator: WorkflowLocator
    node_ids: tuple[str, ...]
    graph: tuple[tuple[str, tuple[str, ...]], ...]
    node_types: tuple[tuple[str, str], ...]
    display_names: tuple[tuple[str, str], ...]
    node_slugs: tuple[tuple[str, str], ...] = ()
    cron: str | None = None


@dataclass(frozen=True)
class CatalogView:
    """One atomically replaceable, current-only registry view."""

    by_id: Mapping[str, WorkflowDescriptor] = field(
        default_factory=lambda: MappingProxyType({})
    )
    short_names: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...] = ()


def display_name_from_id(node_id: str) -> str:
    """Derive display name from a future_id: 'fetch_orders_1' → 'fetch_orders'."""
    parts = node_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return node_id
