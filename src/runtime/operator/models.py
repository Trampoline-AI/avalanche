"""Shared operator/TUI state models with no TUI dependencies."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Literal, Mapping

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

if TYPE_CHECKING:
    from avalanche.agent.declaration import AgentDeclaration
    from avalanche.agent.live import AgentTraceFinished, AgentTraceUnavailable
    from avalanche.agent.native import TraceHeaderNative
    from avalanche.agent.trace import AgentTraceStatus, TokenUsage

    from .step_io import StepIo


# The common kind of an agent live event; "native" when it has no common equivalent.
AgentEventKind = Literal[
    "native",
    "run.started",
    "turn.finished",
    "tool.started",
    "tool.finished",
]


class NodeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(Enum):
    REQUESTING = "requesting"
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _require_finite(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite, not NaN or Infinity")
    if isinstance(value, list):
        for item in value:
            _require_finite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _require_finite(item)


def _finite_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    _require_finite(value)
    return value


# Input a schedule's cron runs start with. Python's JSON tools accept NaN and
# Infinity, which are not JSON and could not be sent back to clients.
ScheduleInput = Annotated[dict[str, JsonValue], AfterValidator(_finite_object)]
_SCHEDULE_INPUT = TypeAdapter(ScheduleInput)


def schedule_input_from_python(value: JsonValue) -> dict[str, JsonValue]:
    """Validate decoded schedule input: one JSON object of finite values."""
    try:
        return _SCHEDULE_INPUT.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"Schedule input must be a JSON object: {exc}") from exc


def schedule_input_from_json(payload: str) -> dict[str, JsonValue]:
    """Decode a schedule input document: one JSON object of finite values."""
    try:
        return _SCHEDULE_INPUT.validate_json(payload)
    except ValidationError as exc:
        raise ValueError(f"Schedule input must be a JSON object: {exc}") from exc


class MissingRunInputError(ValueError):
    """A run or schedule would start without a value for a required input field."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__(f"Input is missing required fields: {', '.join(fields)}")


class _InputSchema(BaseModel):
    """The parts of a workflow's input JSON Schema that say which fields are required."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # A schema without these keywords declares no required fields or definitions.
    required: tuple[str, ...] = ()
    properties: dict[str, JsonValue] = Field(default_factory=dict)
    definitions: dict[str, JsonValue] = Field(default_factory=dict, alias="$defs")


def _rejects_null(schema: JsonValue, definitions: Mapping[str, JsonValue]) -> bool:
    """Whether the schema certainly rejects null; anything undecided accepts it.

    The run's own validation has the final word, so this only refuses input the
    schema clearly forbids, never input Pydantic might accept.
    """
    if not isinstance(schema, dict):
        # `true` accepts everything; `false` accepts nothing.
        return schema is False
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target = definitions.get(reference.removeprefix("#/$defs/"))
        if target is not None and _rejects_null(target, definitions):
            return True
    kind = schema.get("type")
    if isinstance(kind, str) and kind != "null":
        return True
    if isinstance(kind, list) and "null" not in kind:
        return True
    if "const" in schema and schema["const"] is not None:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        return True
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            if all(_rejects_null(branch, definitions) for branch in branches):
                return True
    every = schema.get("allOf")
    return isinstance(every, list) and any(_rejects_null(item, definitions) for item in every)


def missing_required_input(
    input_schema_json: str, run_input: Mapping[str, JsonValue] | None
) -> tuple[str, ...]:
    """Required fields the input leaves out, or sets to null where null is not allowed.

    The operator does not import workflow code, so this checks only presence against
    the published schema; the run still validates every value when it starts.
    """
    if input_schema_json == "":
        return ()
    schema = _InputSchema.model_validate_json(input_schema_json)
    values: Mapping[str, JsonValue] = {} if run_input is None else run_input
    return tuple(
        name
        for name in schema.required
        if name not in values
        or (values[name] is None and _rejects_null(schema.properties[name], schema.definitions))
    )


class _FieldSchema(BaseModel):
    """The parts of an input field's JSON Schema that say whether it holds a list."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # JSON Schema itself allows these forms: `type` is one name or a list of names,
    # and an anyOf branch may be the boolean schema `true` or `false`.
    type: str | tuple[str, ...] | None = None
    any_of: tuple[_FieldSchema | bool, ...] = Field(default=(), alias="anyOf")

    @property
    def holds_list(self) -> bool:
        """An array, alone or as a branch of an optional field such as ``list[File] | None``."""
        types = (self.type,) if isinstance(self.type, str) else self.type or ()
        return "array" in types or any(
            not isinstance(branch, bool) and branch.holds_list for branch in self.any_of
        )


# A property schema may also be the boolean schema `true` or `false`.
_PROPERTY_SCHEMA: TypeAdapter[_FieldSchema | bool] = TypeAdapter(_FieldSchema | bool)


@dataclass(frozen=True)
class InputFileFields:
    """The input fields a workflow declares, by whether each holds one value or a list."""

    declared: frozenset[str]
    lists: frozenset[str]


def input_file_fields(input_schema_json: str) -> InputFileFields:
    """Read a workflow's published input schema once for where input files may go."""
    if input_schema_json == "":
        return InputFileFields(declared=frozenset(), lists=frozenset())
    schema = _InputSchema.model_validate_json(input_schema_json)
    lists: set[str] = set()
    for name, property_schema in schema.properties.items():
        kind = _PROPERTY_SCHEMA.validate_python(property_schema)
        if not isinstance(kind, bool) and kind.holds_list:
            lists.add(name)
    return InputFileFields(declared=frozenset(schema.properties), lists=frozenset(lists))


class TriggerKind(Enum):
    """What started a run: its schedule, a person, a webhook, or an API caller."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    WEBHOOK = "webhook"
    API = "api"


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

    # Operator diagnostics have no worker attempt; captured activity carries both.

    channel: str | None = None
    attempt: str | None = None


@dataclass(frozen=True)
class WorkflowTopology:
    """Immutable rendering metadata captured from one prepared workflow."""

    node_ids: tuple[str, ...] = ()
    graph: tuple[tuple[str, tuple[str, ...]], ...] = ()
    node_types: tuple[tuple[str, str], ...] = ()
    display_names: tuple[tuple[str, str], ...] = ()
    agent_field_schemas_json: tuple[tuple[str, str], ...] = ()
    agent_instruction_lines: tuple[tuple[str, str], ...] = ()
    agent_runtime_kinds: tuple[tuple[str, str], ...] = ()
    standard_step_docstring_lines: tuple[tuple[str, str], ...] = ()
    classifier_metadata_json: tuple[tuple[str, str], ...] = ()
    evaluation_metadata_json: tuple[tuple[str, str], ...] = ()
    step_interface_json: tuple[tuple[str, str], ...] = ()
    # Serialized WorkflowInputDeclaration; empty when the workflow declares no input.
    input_declaration_json: str = ""


@dataclass(frozen=True)
class RunInput:
    """The validated input one run started with."""

    values_json: str
    # Fields the caller supplied; every other field took its declared default.
    supplied_fields: tuple[str, ...] = ()


@dataclass
class NodeState:
    node_id: str
    name: str
    node_type: str  # "source" | "step" | "dest"
    status: NodeStatus = NodeStatus.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None
    step_io: StepIo | None = None
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
    triggered_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    logs: list[LogEntry] = field(default_factory=list)
    triggered_by: TriggerKind = TriggerKind.MANUAL
    workflow_id: str = ""
    workflow_display_name: str = ""
    topology: WorkflowTopology = field(default_factory=WorkflowTopology)
    operator_instance_id: str = ""
    created_sequence: int = 0
    revision: int = 0
    latest_log_sequence: int = 0
    details_hydrated: bool = True  # False only for summary-only list projections.
    terminal_seal: TerminalSealDescriptor | None = None
    input: RunInput | None = None

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return end - self.started_at


@dataclass(frozen=True)
class TraceHeader:
    """The summary of a finished agent invocation, with its runtime-specific part."""

    status: AgentTraceStatus
    model: str | None
    duration_ms: int
    usage: TokenUsage | None
    turn_count: int
    tool_call_count: int
    external_run_id: str | None
    stop_reason: str | None
    native: TraceHeaderNative

    @property
    def runtime_kind(self) -> str:
        return self.native.runtime_kind

    @property
    def trace_format(self) -> str:
        return self.native.format


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
    header: TraceHeader | None = None


@dataclass(frozen=True)
class TerminalSealDescriptor:
    """Immutable terminal activity identity and typed terminal outcome."""

    activity_id: str
    run_sequence: int
    timestamp: datetime
    terminal_status: RunStatus
    reason: str | None = None


@dataclass(frozen=True)
class TraceDetail:
    """Terminal trace/evidence detail pinned to structural run and node identity."""

    operator_instance_id: str
    run_id: str
    created_sequence: int
    node_id: str
    descriptor_revision: int
    trace_body: AgentTraceFinished | AgentTraceUnavailable


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
    step_io: StepIo | None = None
    trace: TraceDescriptor | None = None
    revision: int = 0
    event_page_token: str = ""
    running_elapsed_seconds: float | None = None


@dataclass(frozen=True)
class RunSummary:
    """List-safe run metadata with no logs, agent events, or trace bodies."""

    run_id: str
    flow_name: str
    status: RunStatus = RunStatus.PENDING
    triggered_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    triggered_by: TriggerKind = TriggerKind.MANUAL
    workflow_id: str = ""
    workflow_display_name: str = ""
    created_sequence: int = 0
    revision: int = 0


@dataclass(frozen=True)
class RunSnapshot:
    """Structural run baseline tied to one operator instance and event cursor."""

    operator_instance_id: str
    as_of_sequence: int
    summary: RunSummary
    as_of_event_ulid: str = ""
    nodes: tuple[NodeSnapshot, ...] = ()
    latest_log_sequence: int = 0
    log_page_token: str = ""
    topology: WorkflowTopology = field(default_factory=WorkflowTopology)
    terminal_seal: TerminalSealDescriptor | None = None
    input: RunInput | None = None


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
    native_event_kind: str = ""
    agent_event_kind: AgentEventKind = "native"
    turn: int | None = None
    duration_ms: int | None = None
    error: bool = False
    tool_call_count: int = 0


@dataclass(frozen=True)
class ClassifierChoiceSummary:
    question_id: str
    choice: str


@dataclass(frozen=True)
class ClassifierNoulSummary:
    question_id: str
    noul: float


@dataclass(frozen=True)
class ClassifierScoreSummary:
    question_id: str
    score: float


ClassifierAnswerSummary = (
    ClassifierChoiceSummary | ClassifierNoulSummary | ClassifierScoreSummary
)


@dataclass(frozen=True)
class ClassifierEvent:
    """One classifier invocation snapshot stored outside structural state."""

    invocation_id: str
    invocation_index: int
    answers: tuple[ClassifierAnswerSummary, ...]
    event_sequence: int
    event_json: str
    size_bytes: int = 0
    event_kind: str = ""
    duration_ms: int | None = None
    error: bool = False


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


@dataclass(frozen=True)
class ClassifierEventDetailAppended:
    """One identity-pinned live classifier invocation body."""

    operator_instance_id: str
    run_id: str
    created_sequence: int
    sequence: int
    node_id: str
    event: ClassifierEvent


DetailUpdate = LogDetailAppended | AgentEventDetailAppended | ClassifierEventDetailAppended


@dataclass(frozen=True)
class LogRecordDescriptor:
    """Bounded metadata for a log body fetched through ``ReadActivityDetail``."""

    sequence: int
    timestamp: datetime
    level: LogLevel
    node_id: str
    size_bytes: int
    body_token: str
    channel: str | None = None
    attempt: str | None = None


@dataclass(frozen=True)
class AgentEventDescriptor:
    """Bounded identity, summary, and availability metadata for an agent event body."""

    invocation_id: str
    event_sequence: int
    size_bytes: int
    body_token: str
    native_event_kind: str = ""
    agent_event_kind: AgentEventKind = "native"
    turn: int | None = None
    duration_ms: int | None = None
    error: bool = False
    tool_call_count: int = 0


@dataclass(frozen=True)
class ClassifierEventDescriptor:
    """Identity, compact answers, and availability for a classifier invocation."""

    invocation_id: str
    invocation_index: int
    answers: tuple[ClassifierAnswerSummary, ...]
    event_sequence: int
    size_bytes: int
    body_token: str
    event_kind: str = ""
    duration_ms: int | None = None
    error: bool = False


@dataclass(frozen=True)
class RunSummaryPage:
    """One stable page from the operator-owned run index."""

    operator_instance_id: str
    as_of_sequence: int
    as_of_event_ulid: str = ""
    runs: tuple[RunSummary, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class LogPage:
    """One byte-bounded page of immutable log body descriptors."""

    operator_instance_id: str
    as_of_sequence: int
    as_of_event_ulid: str = ""
    logs: tuple[LogRecordDescriptor, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class AgentEventPage:
    """One byte-bounded page of immutable agent event body descriptors."""

    operator_instance_id: str
    as_of_sequence: int
    run_id: str
    node_id: str
    as_of_event_ulid: str = ""
    events: tuple[AgentEventDescriptor, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class ClassifierEventPage:
    """One byte-bounded page of immutable classifier invocation descriptors."""

    operator_instance_id: str
    as_of_sequence: int
    run_id: str
    node_id: str
    as_of_event_ulid: str = ""
    events: tuple[ClassifierEventDescriptor, ...] = ()
    next_page_token: str = ""


@dataclass(frozen=True)
class FinalizedTrace:
    """Immutable serialized trace body addressable by node revision."""

    revision: int
    data: bytes


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
    as_of_event_ulid: str = ""
    revision: int = 0
    workflows: tuple[WorkflowInfo, ...] = ()
    scan_targets: tuple[ScanTargetInfo, ...] = ()
    diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...] = ()


@dataclass(frozen=True)
class CatalogReplaced:
    """One full catalog publication carried by the operator update stream."""

    catalog: CatalogSnapshot


@dataclass(frozen=True)
class CatalogReloadRequired:
    """A deployment change requires clients to reload their catalog baseline."""

    deployment_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_id, str) or not self.deployment_id:
            raise ValueError("catalog reload notice requires a deployment ID")


@dataclass(frozen=True)
class WorkflowReloadStatus:
    """Whether the operator is rebuilding its workflow catalog."""

    reloading: bool


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
    running_elapsed_seconds: float | None = None
    step_io: StepIo | None = None


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
class ClassifierEventAppended:
    run_id: str
    node_id: str
    event: ClassifierEventDescriptor


@dataclass(frozen=True)
class TraceFinalized:
    run_id: str
    node_id: str
    trace: TraceDescriptor


@dataclass(frozen=True)
class TerminalSealAppended:
    """One structural terminal activity delivered outside status changes."""

    run_id: str
    seal: TerminalSealDescriptor

    @property
    def terminal_seal(self) -> TerminalSealDescriptor:
        """Expose the wire-oriented name without duplicating state."""
        return self.seal


RunUpdateChange = (
    RunCreated
    | RunStatusChanged
    | NodeStatusChanged
    | LogAppended
    | AgentEventAppended
    | ClassifierEventAppended
    | TraceFinalized
    | TerminalSealAppended
)
OperatorUpdateChange = (
    RunUpdateChange | CatalogReplaced | CatalogReloadRequired | WorkflowReloadStatus
)


@dataclass(frozen=True)
class OperatorUpdate:
    sequence: int
    change: OperatorUpdateChange
    event_ulid: str = ""


@dataclass(frozen=True)
class ResetRequired:
    # These numeric values remain private operator retention indexes. V2
    # conversion exposes the server-issued event ULIDs below instead.
    history_floor: int = 0
    latest_sequence: int = 0
    history_floor_event_ulid: str = ""
    latest_event_ulid: str = ""


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
    agent_declarations: dict[str, AgentDeclaration] = field(default_factory=dict)
    classifier_metadata_json: dict[str, str] = field(default_factory=dict)
    evaluation_metadata_json: dict[str, str] = field(default_factory=dict)
    step_interface_json: dict[str, str] = field(default_factory=dict)
    standard_step_docstring_lines: dict[str, str] = field(default_factory=dict)
    node_source_code: dict[str, str] = field(default_factory=dict)
    cron: str | None = None  # cron expression for scheduled execution
    next_run_at: float | None = None  # unix timestamp of next scheduled run
    last_run_at: float | None = None  # unix timestamp of last triggered run
    # True only while the operator will start this workflow's cron runs.
    schedule_enabled: bool = False
    # Input each cron run starts with; None means the workflow's declared defaults.
    schedule_input: dict[str, JsonValue] | None = None
    workflow_id: str = ""
    display_name: str = ""
    builder_symbol: str = ""
    root_alias: str = ""
    relative_file: str = ""
    webhook_path: str | None = None
    webhook_enabled: bool = False
    webhook_url: str | None = None
    webhook_active: bool = False
    input_schema_json: str = ""
    input_declaration_json: str = ""
    version: str = ""

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
    previous_event_ulid: str
    observed_event_ulid: str
    operator_instance_id: str = ""


@dataclass(frozen=True)
class ResetBaselineCursor:
    """Complete server-issued lifecycle cursor for one reset baseline."""

    stream: str
    topology_fingerprint: str
    stream_generation: int
    retained_floor_event_ulid: str
    event_ulid: str


@dataclass(frozen=True)
class ResetBaseline:
    """Authoritative UI baseline associated with one reset generation."""

    generation: int
    operator_instance_id: str
    as_of_event_ulid: str
    catalog: CatalogSnapshot
    runs_by_workflow: Mapping[str, tuple[RunState, ...]]
    cursor: ResetBaselineCursor | None = None


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
    agent_declarations: tuple[tuple[str, AgentDeclaration], ...] = ()
    classifier_metadata_json: tuple[tuple[str, str], ...] = ()
    evaluation_metadata_json: tuple[tuple[str, str], ...] = ()
    step_interface_json: tuple[tuple[str, str], ...] = ()
    standard_step_docstring_lines: tuple[tuple[str, str], ...] = ()
    node_source_code: tuple[tuple[str, str], ...] = ()
    cron: str | None = None
    webhook_path: str | None = None
    webhook_enabled: bool = False
    input_schema_json: str = ""
    input_declaration_json: str = ""


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
