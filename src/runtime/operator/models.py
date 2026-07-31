"""Shared operator/TUI state models with no TUI dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Mapping


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


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    level: LogLevel
    node_id: str
    message: str


@dataclass(frozen=True)
class WorkflowTopology:
    """Immutable rendering metadata captured from one prepared workflow."""

    node_ids: tuple[str, ...] = ()
    graph: tuple[tuple[str, tuple[str, ...]], ...] = ()
    node_types: tuple[tuple[str, str], ...] = ()
    display_names: tuple[tuple[str, str], ...] = ()


@dataclass
class NodeState:
    node_id: str
    name: str
    node_type: str  # "source" | "step" | "dest"
    status: NodeStatus = NodeStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None
    agent_trace_json: str | None = None
    trace: TraceDescriptor | None = None
    revision: int = 0
    event_page_token: str = ""

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
    workflow_id: str = ""
    workflow_display_name: str = ""
    topology: WorkflowTopology = field(default_factory=WorkflowTopology)
    operator_instance_id: str = ""
    created_sequence: int = 0
    revision: int = 0
    latest_log_sequence: int = 0
    details_hydrated: bool = True  # False only for summary-only list projections.

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return end - self.started_at


@dataclass(frozen=True)
class TraceDescriptor:
    """Location metadata for agent detail stored outside structural run state."""

    status: str = "unavailable"
    revision: int = 0
    available: bool = False
    complete: bool = False
    event_count: int = 0
    size_bytes: int = 0
    latest_event_sequence: int = 0


@dataclass(frozen=True)
class TraceDetail:
    """One immutable trace body pinned to structural run and node identity."""

    operator_instance_id: str
    run_id: str
    created_sequence: int
    node_id: str
    descriptor_revision: int
    trace_body: dict[str, Any]


@dataclass(frozen=True)
class NodeSnapshot:
    """Lightweight node state used by baseline reads and run updates."""

    node_id: str
    name: str
    node_type: str
    status: NodeStatus = NodeStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None
    trace: TraceDescriptor | None = None
    revision: int = 0
    event_page_token: str = ""


@dataclass(frozen=True)
class RunSummary:
    """List-safe run metadata with no logs, agent events, or trace bodies."""

    run_id: str
    flow_name: str
    status: RunStatus = RunStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    triggered_by: str = "manual"
    workflow_id: str = ""
    workflow_display_name: str = ""
    created_sequence: int = 0
    revision: int = 0


@dataclass(frozen=True)
class RunSnapshot:
    """Structural run baseline tied to one operator instance and sequence."""

    operator_instance_id: str
    as_of_sequence: int
    summary: RunSummary
    nodes: tuple[NodeSnapshot, ...] = ()
    latest_log_sequence: int = 0
    log_page_token: str = ""
    topology: WorkflowTopology = field(default_factory=WorkflowTopology)


@dataclass(frozen=True)
class SequencedLogEntry:
    """One append-only log record addressable across replay and pagination."""

    sequence: int
    entry: LogEntry
    size_bytes: int = 0


@dataclass(frozen=True)
class AgentEvent:
    """One projected agent event stored independently of structural state."""

    invocation_id: str
    event_sequence: int
    event_json: str
    size_bytes: int = 0
    event_kind: str = ""
    iteration: int | None = None
    duration_ms: int | None = None
    error: bool = False
    tool_count: int = 0
    predict_count: int = 0


@dataclass(frozen=True)
class LogDetailAppended:
    """One identity-pinned live log body delivered outside structural state."""

    operator_instance_id: str
    run_id: str
    created_sequence: int
    sequence: int
    log_sequence: int
    log: LogEntry


@dataclass(frozen=True)
class AgentEventDetailAppended:
    """One identity-pinned live agent event body delivered outside structural state."""

    operator_instance_id: str
    run_id: str
    created_sequence: int
    sequence: int
    node_id: str
    event: AgentEvent


DetailUpdate = LogDetailAppended | AgentEventDetailAppended


@dataclass(frozen=True)
class LogRecordDescriptor:
    """Bounded metadata for a log body fetched through ``ReadDetail``."""

    sequence: int
    timestamp: datetime
    level: LogLevel
    node_id: str
    size_bytes: int
    body_token: str


@dataclass(frozen=True)
class AgentEventDescriptor:
    """Bounded identity, summary, and availability metadata for an agent event body."""

    invocation_id: str
    event_sequence: int
    size_bytes: int
    body_token: str
    event_kind: str = ""
    iteration: int | None = None
    duration_ms: int | None = None
    error: bool = False
    tool_count: int = 0
    predict_count: int = 0


@dataclass(frozen=True)
class RunSummaryPage:
    """One stable page from the operator-owned run index."""

    operator_instance_id: str
    as_of_sequence: int
    runs: tuple[RunSummary, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class LogPage:
    """One byte-bounded page of immutable log body descriptors."""

    operator_instance_id: str
    as_of_sequence: int
    logs: tuple[LogRecordDescriptor, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class AgentEventPage:
    """One byte-bounded page of immutable agent event body descriptors."""

    operator_instance_id: str
    as_of_sequence: int
    run_id: str
    node_id: str
    events: tuple[AgentEventDescriptor, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class FinalizedTrace:
    """Immutable serialized trace body addressable by node revision."""

    revision: int
    data: bytes


@dataclass(frozen=True)
class TraceHeader:
    """RunTrace metadata retained separately from iteration and evidence bodies."""

    status: str
    model: str
    sub_model: str | None
    iterations: int
    max_iterations: int
    duration_ms: int
    usage_json: str
    telemetry_json: str | None = None


@dataclass(frozen=True)
class ScanTargetInfo:
    """One normalized workflow discovery target exposed to clients."""

    alias: str
    target_path: str
    kind: Literal["file", "directory"]


@dataclass(frozen=True)
class CatalogSnapshot:
    """One complete authoritative current-workflow catalog projection."""

    operator_instance_id: str = ""
    as_of_sequence: int = 0
    revision: int = 0
    workflows: tuple[WorkflowInfo, ...] = ()
    scan_targets: tuple[ScanTargetInfo, ...] = ()
    diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...] = ()


@dataclass(frozen=True)
class CatalogReplaced:
    """One full catalog publication carried by the operator update stream."""

    catalog: CatalogSnapshot


@dataclass(frozen=True)
class RunCreated:
    summary: RunSummary
    nodes: tuple[NodeSnapshot, ...] = ()
    topology: WorkflowTopology = field(default_factory=WorkflowTopology)


@dataclass(frozen=True)
class RunStatusChanged:
    run_id: str
    status: RunStatus
    started_at: float | None = None
    ended_at: float | None = None
    revision: int = 0


@dataclass(frozen=True)
class NodeStatusChanged:
    run_id: str
    node_id: str
    status: NodeStatus
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class LogAppended:
    run_id: str
    log: LogRecordDescriptor


@dataclass(frozen=True)
class AgentEventAppended:
    run_id: str
    node_id: str
    event: AgentEventDescriptor


@dataclass(frozen=True)
class TraceFinalized:
    run_id: str
    node_id: str
    trace: TraceDescriptor


RunUpdateChange = (
    RunCreated
    | RunStatusChanged
    | NodeStatusChanged
    | LogAppended
    | AgentEventAppended
    | TraceFinalized
)
OperatorUpdateChange = RunUpdateChange | CatalogReplaced


@dataclass(frozen=True)
class OperatorUpdate:
    sequence: int
    change: OperatorUpdateChange


@dataclass(frozen=True)
class ResetRequired:
    history_floor: int
    latest_sequence: int


@dataclass(frozen=True)
class OperatorUpdateEnvelope:
    operator_instance_id: str
    update: OperatorUpdate | None = None
    reset_required: ResetRequired | None = None

    def __post_init__(self) -> None:
        if (self.update is None) == (self.reset_required is None):
            raise ValueError("operator update envelope requires exactly one payload")


@dataclass
class WorkflowInfo:
    """Flat snapshot of a workflow — the TUI never holds real Workflow objects."""

    name: str
    file_path: str
    node_ids: list[str]  # topological order (future_ids, e.g. "fetch_orders_1")
    graph: dict[str, list[str]]  # adjacency list (parent -> children)
    node_types: dict[str, str]  # node_id -> "source" | "step" | "dest"
    display_names: dict[str, str] = field(default_factory=dict)  # node_id -> display name
    agent_node_ids: list[str] = field(default_factory=list)
    agent_metadata_json: dict[str, str] = field(default_factory=dict)
    cron: str | None = None  # cron expression for scheduled execution
    next_run_at: float | None = None  # unix timestamp of next scheduled run
    last_run_at: float | None = None  # unix timestamp of last triggered run
    workflow_id: str = ""
    display_name: str = ""
    builder_symbol: str = ""
    root_alias: str = ""
    relative_file: str = ""
    webhook_path: str | None = None
    webhook_enabled: bool = False
    webhook_url: str | None = None
    webhook_active: bool = False

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
class StreamResetNotice:
    """One client-local reset generation caused by a stream epoch change."""

    generation: int
    previous_sequence: int
    observed_sequence: int
    operator_instance_id: str = ""


@dataclass(frozen=True)
class ResetBaseline:
    """Authoritative UI baseline associated with one reset generation."""

    generation: int
    operator_instance_id: str
    as_of_sequence: int
    catalog: CatalogSnapshot
    runs_by_workflow: Mapping[str, tuple[RunState, ...]]


@dataclass(frozen=True)
class WorkflowDiscoveryDiagnostic:
    path: str
    kind: Literal[
        "skipped", "import_error", "build_error", "invalid_schedule", "invalid_catalog"
    ]
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
    agent_node_ids: tuple[str, ...] = ()
    agent_metadata_json: tuple[tuple[str, str], ...] = ()
    cron: str | None = None
    webhook_path: str | None = None
    webhook_enabled: bool = False


@dataclass(frozen=True)
class CatalogView:
    """One atomically replaceable current-workflow registry view."""

    revision: int = 0
    by_id: Mapping[str, WorkflowDescriptor] = field(
        default_factory=lambda: MappingProxyType({})
    )
    short_names: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    scan_targets: tuple[ScanTargetInfo, ...] = ()
    diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...] = ()


def display_name_from_id(node_id: str) -> str:
    """Derive display name from a future_id: 'fetch_orders_1' → 'fetch_orders'."""
    parts = node_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return node_id
