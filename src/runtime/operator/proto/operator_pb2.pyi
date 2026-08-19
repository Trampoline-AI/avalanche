from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PageOrderV2(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PAGE_ORDER_V2_FORWARD: _ClassVar[PageOrderV2]
    PAGE_ORDER_V2_NEWEST_FIRST: _ClassVar[PageOrderV2]
PAGE_ORDER_V2_FORWARD: PageOrderV2
PAGE_ORDER_V2_NEWEST_FIRST: PageOrderV2

class ScopeReferenceV2(_message.Message):
    __slots__ = ("reference",)
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    reference: str
    def __init__(self, reference: _Optional[str] = ...) -> None: ...

class LifecycleCursorV2(_message.Message):
    __slots__ = ("stream", "topology_fingerprint", "stream_generation", "retained_floor_event_ulid", "event_ulid")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    TOPOLOGY_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    STREAM_GENERATION_FIELD_NUMBER: _ClassVar[int]
    RETAINED_FLOOR_EVENT_ULID_FIELD_NUMBER: _ClassVar[int]
    EVENT_ULID_FIELD_NUMBER: _ClassVar[int]
    stream: str
    topology_fingerprint: str
    stream_generation: int
    retained_floor_event_ulid: str
    event_ulid: str
    def __init__(self, stream: _Optional[str] = ..., topology_fingerprint: _Optional[str] = ..., stream_generation: _Optional[int] = ..., retained_floor_event_ulid: _Optional[str] = ..., event_ulid: _Optional[str] = ...) -> None: ...

class ProjectSummaryCursorV2(_message.Message):
    __slots__ = ("stream", "topology_fingerprint", "source_generation", "retained_floor_sequence", "target_head_sequence", "checkpoint_watermark", "checkpoint_digest")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    TOPOLOGY_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    RETAINED_FLOOR_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    TARGET_HEAD_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_WATERMARK_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    stream: str
    topology_fingerprint: str
    source_generation: str
    retained_floor_sequence: int
    target_head_sequence: int
    checkpoint_watermark: int
    checkpoint_digest: str
    def __init__(self, stream: _Optional[str] = ..., topology_fingerprint: _Optional[str] = ..., source_generation: _Optional[str] = ..., retained_floor_sequence: _Optional[int] = ..., target_head_sequence: _Optional[int] = ..., checkpoint_watermark: _Optional[int] = ..., checkpoint_digest: _Optional[str] = ...) -> None: ...

class ContinuationRefV2(_message.Message):
    __slots__ = ("scope_ref", "continuation_id", "cursor", "project_summary_cursor")
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    CONTINUATION_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    PROJECT_SUMMARY_CURSOR_FIELD_NUMBER: _ClassVar[int]
    scope_ref: ScopeReferenceV2
    continuation_id: str
    cursor: LifecycleCursorV2
    project_summary_cursor: ProjectSummaryCursorV2
    def __init__(self, scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., continuation_id: _Optional[str] = ..., cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., project_summary_cursor: _Optional[_Union[ProjectSummaryCursorV2, _Mapping]] = ...) -> None: ...

class DiscoverFlowsRequestV2(_message.Message):
    __slots__ = ("page_size", "continuation")
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    page_size: int
    continuation: ContinuationRefV2
    def __init__(self, page_size: _Optional[int] = ..., continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ...) -> None: ...

class FlowInfoV2(_message.Message):
    __slots__ = ("workflow_selector", "display_name", "manifest_digest", "node_ids", "workflow_id", "file_path", "topology", "agent_node_ids", "agent_metadata_json", "cron", "next_run_at", "last_run_at", "webhook_path", "webhook_url", "webhook_active")
    class AgentMetadataJsonEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_DIGEST_FIELD_NUMBER: _ClassVar[int]
    NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_ID_FIELD_NUMBER: _ClassVar[int]
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    TOPOLOGY_FIELD_NUMBER: _ClassVar[int]
    AGENT_NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    AGENT_METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    CRON_FIELD_NUMBER: _ClassVar[int]
    NEXT_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_PATH_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_URL_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    workflow_selector: str
    display_name: str
    manifest_digest: str
    node_ids: _containers.RepeatedScalarFieldContainer[str]
    workflow_id: str
    file_path: str
    topology: WorkflowTopologyV2
    agent_node_ids: _containers.RepeatedScalarFieldContainer[str]
    agent_metadata_json: _containers.ScalarMap[str, str]
    cron: str
    next_run_at: float
    last_run_at: float
    webhook_path: str
    webhook_url: str
    webhook_active: bool
    def __init__(self, workflow_selector: _Optional[str] = ..., display_name: _Optional[str] = ..., manifest_digest: _Optional[str] = ..., node_ids: _Optional[_Iterable[str]] = ..., workflow_id: _Optional[str] = ..., file_path: _Optional[str] = ..., topology: _Optional[_Union[WorkflowTopologyV2, _Mapping]] = ..., agent_node_ids: _Optional[_Iterable[str]] = ..., agent_metadata_json: _Optional[_Mapping[str, str]] = ..., cron: _Optional[str] = ..., next_run_at: _Optional[float] = ..., last_run_at: _Optional[float] = ..., webhook_path: _Optional[str] = ..., webhook_url: _Optional[str] = ..., webhook_active: bool = ...) -> None: ...

class DiscoveryDiagnosticV2(_message.Message):
    __slots__ = ("path", "kind", "message")
    PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    path: str
    kind: str
    message: str
    def __init__(self, path: _Optional[str] = ..., kind: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class FlowListV2(_message.Message):
    __slots__ = ("cursor", "flows", "next_page", "diagnostics", "scan_targets", "scope_ref", "revision")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    FLOWS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    SCAN_TARGETS_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    flows: _containers.RepeatedCompositeFieldContainer[FlowInfoV2]
    next_page: ContinuationRefV2
    diagnostics: _containers.RepeatedCompositeFieldContainer[DiscoveryDiagnosticV2]
    scan_targets: _containers.RepeatedCompositeFieldContainer[ScanTargetV2]
    scope_ref: ScopeReferenceV2
    revision: int
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., flows: _Optional[_Iterable[_Union[FlowInfoV2, _Mapping]]] = ..., next_page: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., diagnostics: _Optional[_Iterable[_Union[DiscoveryDiagnosticV2, _Mapping]]] = ..., scan_targets: _Optional[_Iterable[_Union[ScanTargetV2, _Mapping]]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., revision: _Optional[int] = ...) -> None: ...

class NodeEdgesV2(_message.Message):
    __slots__ = ("children",)
    CHILDREN_FIELD_NUMBER: _ClassVar[int]
    children: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, children: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkflowTopologyV2(_message.Message):
    __slots__ = ("node_ids", "graph", "node_types", "display_names", "agent_field_schemas_json", "agent_instruction_lines")
    class GraphEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: NodeEdgesV2
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[NodeEdgesV2, _Mapping]] = ...) -> None: ...
    class NodeTypesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class DisplayNamesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class AgentFieldSchemasJsonEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class AgentInstructionLinesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPES_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAMES_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_SCHEMAS_JSON_FIELD_NUMBER: _ClassVar[int]
    AGENT_INSTRUCTION_LINES_FIELD_NUMBER: _ClassVar[int]
    node_ids: _containers.RepeatedScalarFieldContainer[str]
    graph: _containers.MessageMap[str, NodeEdgesV2]
    node_types: _containers.ScalarMap[str, str]
    display_names: _containers.ScalarMap[str, str]
    agent_field_schemas_json: _containers.ScalarMap[str, str]
    agent_instruction_lines: _containers.ScalarMap[str, str]
    def __init__(self, node_ids: _Optional[_Iterable[str]] = ..., graph: _Optional[_Mapping[str, NodeEdgesV2]] = ..., node_types: _Optional[_Mapping[str, str]] = ..., display_names: _Optional[_Mapping[str, str]] = ..., agent_field_schemas_json: _Optional[_Mapping[str, str]] = ..., agent_instruction_lines: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ScanTargetV2(_message.Message):
    __slots__ = ("alias", "target_path", "kind")
    ALIAS_FIELD_NUMBER: _ClassVar[int]
    TARGET_PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    alias: str
    target_path: str
    kind: str
    def __init__(self, alias: _Optional[str] = ..., target_path: _Optional[str] = ..., kind: _Optional[str] = ...) -> None: ...

class FileAttachmentV2(_message.Message):
    __slots__ = ("attachment_id", "field_name", "name", "media_type", "object_uri", "object_key", "sha256", "size_bytes", "inline_bytes")
    ATTACHMENT_ID_FIELD_NUMBER: _ClassVar[int]
    FIELD_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_URI_FIELD_NUMBER: _ClassVar[int]
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    INLINE_BYTES_FIELD_NUMBER: _ClassVar[int]
    attachment_id: str
    field_name: str
    name: str
    media_type: str
    object_uri: str
    object_key: str
    sha256: str
    size_bytes: int
    inline_bytes: bytes
    def __init__(self, attachment_id: _Optional[str] = ..., field_name: _Optional[str] = ..., name: _Optional[str] = ..., media_type: _Optional[str] = ..., object_uri: _Optional[str] = ..., object_key: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ..., inline_bytes: _Optional[bytes] = ...) -> None: ...

class StartRunRequestV2(_message.Message):
    __slots__ = ("run_id", "workflow_selector", "input_json", "context_json", "input_files")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    INPUT_JSON_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_JSON_FIELD_NUMBER: _ClassVar[int]
    INPUT_FILES_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    workflow_selector: str
    input_json: str
    context_json: str
    input_files: _containers.RepeatedCompositeFieldContainer[FileAttachmentV2]
    def __init__(self, run_id: _Optional[str] = ..., workflow_selector: _Optional[str] = ..., input_json: _Optional[str] = ..., context_json: _Optional[str] = ..., input_files: _Optional[_Iterable[_Union[FileAttachmentV2, _Mapping]]] = ...) -> None: ...

class StartRunResponseV2(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class CancelRunRequestV2(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class CancelRunResponseV2(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class RunSummaryV2(_message.Message):
    __slots__ = ("run_id", "workflow_selector", "workflow_display_name", "status", "started_at", "ended_at", "created_sequence", "revision", "triggered_by", "triggered_at")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    CREATED_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    TRIGGERED_BY_FIELD_NUMBER: _ClassVar[int]
    TRIGGERED_AT_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    workflow_selector: str
    workflow_display_name: str
    status: str
    started_at: float
    ended_at: float
    created_sequence: int
    revision: int
    triggered_by: str
    triggered_at: float
    def __init__(self, run_id: _Optional[str] = ..., workflow_selector: _Optional[str] = ..., workflow_display_name: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., created_sequence: _Optional[int] = ..., revision: _Optional[int] = ..., triggered_by: _Optional[str] = ..., triggered_at: _Optional[float] = ...) -> None: ...

class ListRunSummariesRequestV2(_message.Message):
    __slots__ = ("workflow_selector", "page_size", "continuation")
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    workflow_selector: str
    page_size: int
    continuation: ContinuationRefV2
    def __init__(self, workflow_selector: _Optional[str] = ..., page_size: _Optional[int] = ..., continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ...) -> None: ...

class RunSummaryPageV2(_message.Message):
    __slots__ = ("cursor", "runs", "next_page", "scope_ref", "project_summary_cursor")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    RUNS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    PROJECT_SUMMARY_CURSOR_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    runs: _containers.RepeatedCompositeFieldContainer[RunSummaryV2]
    next_page: ContinuationRefV2
    scope_ref: ScopeReferenceV2
    project_summary_cursor: ProjectSummaryCursorV2
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., runs: _Optional[_Iterable[_Union[RunSummaryV2, _Mapping]]] = ..., next_page: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., project_summary_cursor: _Optional[_Union[ProjectSummaryCursorV2, _Mapping]] = ...) -> None: ...

class GetRunSnapshotRequestV2(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class NodeSnapshotV2(_message.Message):
    __slots__ = ("node_id", "name", "node_type", "status", "started_at", "ended_at", "revision", "error", "running_elapsed_seconds", "trace", "activity_continuation")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RUNNING_ELAPSED_SECONDS_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    name: str
    node_type: str
    status: str
    started_at: float
    ended_at: float
    revision: int
    error: str
    running_elapsed_seconds: float
    trace: TraceDescriptorV2
    activity_continuation: ContinuationRefV2
    def __init__(self, node_id: _Optional[str] = ..., name: _Optional[str] = ..., node_type: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., revision: _Optional[int] = ..., error: _Optional[str] = ..., running_elapsed_seconds: _Optional[float] = ..., trace: _Optional[_Union[TraceDescriptorV2, _Mapping]] = ..., activity_continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ...) -> None: ...

class TraceHeaderV2(_message.Message):
    __slots__ = ("status", "model", "sub_model", "iterations", "max_iterations", "duration_ms", "usage_json", "telemetry_json")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SUB_MODEL_FIELD_NUMBER: _ClassVar[int]
    ITERATIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_ITERATIONS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    USAGE_JSON_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_JSON_FIELD_NUMBER: _ClassVar[int]
    status: str
    model: str
    sub_model: str
    iterations: int
    max_iterations: int
    duration_ms: int
    usage_json: str
    telemetry_json: str
    def __init__(self, status: _Optional[str] = ..., model: _Optional[str] = ..., sub_model: _Optional[str] = ..., iterations: _Optional[int] = ..., max_iterations: _Optional[int] = ..., duration_ms: _Optional[int] = ..., usage_json: _Optional[str] = ..., telemetry_json: _Optional[str] = ...) -> None: ...

class TraceDescriptorV2(_message.Message):
    __slots__ = ("status", "revision", "available", "complete", "event_count", "size_bytes", "latest_event_sequence", "header", "detail_ref")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    EVENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    LATEST_EVENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    DETAIL_REF_FIELD_NUMBER: _ClassVar[int]
    status: str
    revision: int
    available: bool
    complete: bool
    event_count: int
    size_bytes: int
    latest_event_sequence: int
    header: TraceHeaderV2
    detail_ref: ActivityDetailRefV2
    def __init__(self, status: _Optional[str] = ..., revision: _Optional[int] = ..., available: bool = ..., complete: bool = ..., event_count: _Optional[int] = ..., size_bytes: _Optional[int] = ..., latest_event_sequence: _Optional[int] = ..., header: _Optional[_Union[TraceHeaderV2, _Mapping]] = ..., detail_ref: _Optional[_Union[ActivityDetailRefV2, _Mapping]] = ...) -> None: ...

class TerminalSealV2(_message.Message):
    __slots__ = ("terminal_status", "reason")
    TERMINAL_STATUS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    terminal_status: str
    reason: str
    def __init__(self, terminal_status: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class RunSnapshotV2(_message.Message):
    __slots__ = ("cursor", "summary", "nodes", "topology", "scope_ref", "latest_log_sequence", "log_continuation", "terminal_seal")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    TOPOLOGY_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    LATEST_LOG_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    LOG_CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_SEAL_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    summary: RunSummaryV2
    nodes: _containers.RepeatedCompositeFieldContainer[NodeSnapshotV2]
    topology: WorkflowTopologyV2
    scope_ref: ScopeReferenceV2
    latest_log_sequence: int
    log_continuation: ContinuationRefV2
    terminal_seal: RunActivityDescriptorV2
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., summary: _Optional[_Union[RunSummaryV2, _Mapping]] = ..., nodes: _Optional[_Iterable[_Union[NodeSnapshotV2, _Mapping]]] = ..., topology: _Optional[_Union[WorkflowTopologyV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., latest_log_sequence: _Optional[int] = ..., log_continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., terminal_seal: _Optional[_Union[RunActivityDescriptorV2, _Mapping]] = ...) -> None: ...

class ActivityDetailRefV2(_message.Message):
    __slots__ = ("run_id", "scope_ref", "activity_id", "run_sequence", "object_uri", "object_key", "sha256", "size_bytes")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_URI_FIELD_NUMBER: _ClassVar[int]
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    scope_ref: ScopeReferenceV2
    activity_id: str
    run_sequence: int
    object_uri: str
    object_key: str
    sha256: str
    size_bytes: int
    def __init__(self, run_id: _Optional[str] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., activity_id: _Optional[str] = ..., run_sequence: _Optional[int] = ..., object_uri: _Optional[str] = ..., object_key: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class RunActivityDescriptorV2(_message.Message):
    __slots__ = ("activity_id", "run_sequence", "kind", "timestamp", "size_bytes", "detail_ref", "node_id", "level", "invocation_id", "iteration", "duration_ms", "error", "tool_count", "predict_count", "event_kind", "trace", "terminal_seal")
    ACTIVITY_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    DETAIL_REF_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    ITERATION_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TOOL_COUNT_FIELD_NUMBER: _ClassVar[int]
    PREDICT_COUNT_FIELD_NUMBER: _ClassVar[int]
    EVENT_KIND_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_SEAL_FIELD_NUMBER: _ClassVar[int]
    activity_id: str
    run_sequence: int
    kind: str
    timestamp: float
    size_bytes: int
    detail_ref: ActivityDetailRefV2
    node_id: str
    level: str
    invocation_id: str
    iteration: int
    duration_ms: int
    error: bool
    tool_count: int
    predict_count: int
    event_kind: str
    trace: TraceDescriptorV2
    terminal_seal: TerminalSealV2
    def __init__(self, activity_id: _Optional[str] = ..., run_sequence: _Optional[int] = ..., kind: _Optional[str] = ..., timestamp: _Optional[float] = ..., size_bytes: _Optional[int] = ..., detail_ref: _Optional[_Union[ActivityDetailRefV2, _Mapping]] = ..., node_id: _Optional[str] = ..., level: _Optional[str] = ..., invocation_id: _Optional[str] = ..., iteration: _Optional[int] = ..., duration_ms: _Optional[int] = ..., error: bool = ..., tool_count: _Optional[int] = ..., predict_count: _Optional[int] = ..., event_kind: _Optional[str] = ..., trace: _Optional[_Union[TraceDescriptorV2, _Mapping]] = ..., terminal_seal: _Optional[_Union[TerminalSealV2, _Mapping]] = ...) -> None: ...

class ListRunActivityRequestV2(_message.Message):
    __slots__ = ("run_id", "page_size", "continuation", "node_id", "order")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    page_size: int
    continuation: ContinuationRefV2
    node_id: str
    order: PageOrderV2
    def __init__(self, run_id: _Optional[str] = ..., page_size: _Optional[int] = ..., continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., node_id: _Optional[str] = ..., order: _Optional[_Union[PageOrderV2, str]] = ...) -> None: ...

class RunActivityPageV2(_message.Message):
    __slots__ = ("cursor", "run_id", "activities", "next_page", "scope_ref")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVITIES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    run_id: str
    activities: _containers.RepeatedCompositeFieldContainer[RunActivityDescriptorV2]
    next_page: ContinuationRefV2
    scope_ref: ScopeReferenceV2
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., run_id: _Optional[str] = ..., activities: _Optional[_Iterable[_Union[RunActivityDescriptorV2, _Mapping]]] = ..., next_page: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ...) -> None: ...

class ReadActivityDetailRequestV2(_message.Message):
    __slots__ = ("detail_ref",)
    DETAIL_REF_FIELD_NUMBER: _ClassVar[int]
    detail_ref: ActivityDetailRefV2
    def __init__(self, detail_ref: _Optional[_Union[ActivityDetailRefV2, _Mapping]] = ...) -> None: ...

class ActivityDetailChunkV2(_message.Message):
    __slots__ = ("chunk_index", "data", "eof")
    CHUNK_INDEX_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EOF_FIELD_NUMBER: _ClassVar[int]
    chunk_index: int
    data: bytes
    eof: bool
    def __init__(self, chunk_index: _Optional[int] = ..., data: _Optional[bytes] = ..., eof: bool = ...) -> None: ...

class RunOutputArtifactRefV2(_message.Message):
    __slots__ = ("run_id", "scope_ref", "artifact_id", "run_sequence", "object_uri", "object_key", "sha256", "size_bytes")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_URI_FIELD_NUMBER: _ClassVar[int]
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    scope_ref: ScopeReferenceV2
    artifact_id: str
    run_sequence: int
    object_uri: str
    object_key: str
    sha256: str
    size_bytes: int
    def __init__(self, run_id: _Optional[str] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ..., artifact_id: _Optional[str] = ..., run_sequence: _Optional[int] = ..., object_uri: _Optional[str] = ..., object_key: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class ResultValueV2(_message.Message):
    __slots__ = ("value_json", "sha256", "size_bytes")
    VALUE_JSON_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    value_json: str
    sha256: str
    size_bytes: int
    def __init__(self, value_json: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class ResultFileDescriptorV2(_message.Message):
    __slots__ = ("artifact_ref", "name", "media_type")
    ARTIFACT_REF_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    artifact_ref: RunOutputArtifactRefV2
    name: str
    media_type: str
    def __init__(self, artifact_ref: _Optional[_Union[RunOutputArtifactRefV2, _Mapping]] = ..., name: _Optional[str] = ..., media_type: _Optional[str] = ...) -> None: ...

class GetRunResultRequestV2(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class RunResultV2(_message.Message):
    __slots__ = ("cursor", "run_id", "value", "files", "scope_ref")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    run_id: str
    value: ResultValueV2
    files: _containers.RepeatedCompositeFieldContainer[ResultFileDescriptorV2]
    scope_ref: ScopeReferenceV2
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., run_id: _Optional[str] = ..., value: _Optional[_Union[ResultValueV2, _Mapping]] = ..., files: _Optional[_Iterable[_Union[ResultFileDescriptorV2, _Mapping]]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ...) -> None: ...

class RunOutputArtifactDescriptorV2(_message.Message):
    __slots__ = ("artifact_ref", "name", "media_type")
    ARTIFACT_REF_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    artifact_ref: RunOutputArtifactRefV2
    name: str
    media_type: str
    def __init__(self, artifact_ref: _Optional[_Union[RunOutputArtifactRefV2, _Mapping]] = ..., name: _Optional[str] = ..., media_type: _Optional[str] = ...) -> None: ...

class ListRunOutputArtifactsRequestV2(_message.Message):
    __slots__ = ("run_id", "page_size", "continuation")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    CONTINUATION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    page_size: int
    continuation: ContinuationRefV2
    def __init__(self, run_id: _Optional[str] = ..., page_size: _Optional[int] = ..., continuation: _Optional[_Union[ContinuationRefV2, _Mapping]] = ...) -> None: ...

class RunOutputArtifactPageV2(_message.Message):
    __slots__ = ("cursor", "run_id", "artifacts", "next_page", "scope_ref")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    cursor: LifecycleCursorV2
    run_id: str
    artifacts: _containers.RepeatedCompositeFieldContainer[RunOutputArtifactDescriptorV2]
    next_page: ContinuationRefV2
    scope_ref: ScopeReferenceV2
    def __init__(self, cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., run_id: _Optional[str] = ..., artifacts: _Optional[_Iterable[_Union[RunOutputArtifactDescriptorV2, _Mapping]]] = ..., next_page: _Optional[_Union[ContinuationRefV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ...) -> None: ...

class ReadRunOutputArtifactRequestV2(_message.Message):
    __slots__ = ("artifact_ref",)
    ARTIFACT_REF_FIELD_NUMBER: _ClassVar[int]
    artifact_ref: RunOutputArtifactRefV2
    def __init__(self, artifact_ref: _Optional[_Union[RunOutputArtifactRefV2, _Mapping]] = ...) -> None: ...

class RunOutputArtifactChunkV2(_message.Message):
    __slots__ = ("chunk_index", "data", "eof")
    CHUNK_INDEX_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EOF_FIELD_NUMBER: _ClassVar[int]
    chunk_index: int
    data: bytes
    eof: bool
    def __init__(self, chunk_index: _Optional[int] = ..., data: _Optional[bytes] = ..., eof: bool = ...) -> None: ...

class WatchRunStatusRequestV2(_message.Message):
    __slots__ = ("after_cursor", "scope_ref")
    AFTER_CURSOR_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    after_cursor: LifecycleCursorV2
    scope_ref: ScopeReferenceV2
    def __init__(self, after_cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ...) -> None: ...

class RunCreatedV2(_message.Message):
    __slots__ = ("summary", "nodes", "topology")
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    TOPOLOGY_FIELD_NUMBER: _ClassVar[int]
    summary: RunSummaryV2
    nodes: _containers.RepeatedCompositeFieldContainer[NodeSnapshotV2]
    topology: WorkflowTopologyV2
    def __init__(self, summary: _Optional[_Union[RunSummaryV2, _Mapping]] = ..., nodes: _Optional[_Iterable[_Union[NodeSnapshotV2, _Mapping]]] = ..., topology: _Optional[_Union[WorkflowTopologyV2, _Mapping]] = ...) -> None: ...

class RunStatusChangedV2(_message.Message):
    __slots__ = ("summary",)
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    summary: RunSummaryV2
    def __init__(self, summary: _Optional[_Union[RunSummaryV2, _Mapping]] = ...) -> None: ...

class NodeStatusChangedV2(_message.Message):
    __slots__ = ("run_id", "node")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    node: NodeSnapshotV2
    def __init__(self, run_id: _Optional[str] = ..., node: _Optional[_Union[NodeSnapshotV2, _Mapping]] = ...) -> None: ...

class ActivityAppendedV2(_message.Message):
    __slots__ = ("run_id", "activity")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    activity: RunActivityDescriptorV2
    def __init__(self, run_id: _Optional[str] = ..., activity: _Optional[_Union[RunActivityDescriptorV2, _Mapping]] = ...) -> None: ...

class FlowListChangedV2(_message.Message):
    __slots__ = ("flow_list",)
    FLOW_LIST_FIELD_NUMBER: _ClassVar[int]
    flow_list: FlowListV2
    def __init__(self, flow_list: _Optional[_Union[FlowListV2, _Mapping]] = ...) -> None: ...

class CatalogReloadRequiredV2(_message.Message):
    __slots__ = ("deployment_id",)
    DEPLOYMENT_ID_FIELD_NUMBER: _ClassVar[int]
    deployment_id: str
    def __init__(self, deployment_id: _Optional[str] = ...) -> None: ...

class FlowReloadStatusV2(_message.Message):
    __slots__ = ("reloading",)
    RELOADING_FIELD_NUMBER: _ClassVar[int]
    reloading: bool
    def __init__(self, reloading: bool = ...) -> None: ...

class ResetRequiredV2(_message.Message):
    __slots__ = ("history_floor", "latest_cursor")
    HISTORY_FLOOR_FIELD_NUMBER: _ClassVar[int]
    LATEST_CURSOR_FIELD_NUMBER: _ClassVar[int]
    history_floor: LifecycleCursorV2
    latest_cursor: LifecycleCursorV2
    def __init__(self, history_floor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., latest_cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ...) -> None: ...

class RunStatusEnvelopeV2(_message.Message):
    __slots__ = ("event_ulid", "run_created", "run_status_changed", "reset_required", "node_status_changed", "activity_appended", "flow_list_changed", "flow_reload_status", "catalog_reload_required", "cursor", "scope_ref")
    EVENT_ULID_FIELD_NUMBER: _ClassVar[int]
    RUN_CREATED_FIELD_NUMBER: _ClassVar[int]
    RUN_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    RESET_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    NODE_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_APPENDED_FIELD_NUMBER: _ClassVar[int]
    FLOW_LIST_CHANGED_FIELD_NUMBER: _ClassVar[int]
    FLOW_RELOAD_STATUS_FIELD_NUMBER: _ClassVar[int]
    CATALOG_RELOAD_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    event_ulid: str
    run_created: RunCreatedV2
    run_status_changed: RunStatusChangedV2
    reset_required: ResetRequiredV2
    node_status_changed: NodeStatusChangedV2
    activity_appended: ActivityAppendedV2
    flow_list_changed: FlowListChangedV2
    flow_reload_status: FlowReloadStatusV2
    catalog_reload_required: CatalogReloadRequiredV2
    cursor: LifecycleCursorV2
    scope_ref: ScopeReferenceV2
    def __init__(self, event_ulid: _Optional[str] = ..., run_created: _Optional[_Union[RunCreatedV2, _Mapping]] = ..., run_status_changed: _Optional[_Union[RunStatusChangedV2, _Mapping]] = ..., reset_required: _Optional[_Union[ResetRequiredV2, _Mapping]] = ..., node_status_changed: _Optional[_Union[NodeStatusChangedV2, _Mapping]] = ..., activity_appended: _Optional[_Union[ActivityAppendedV2, _Mapping]] = ..., flow_list_changed: _Optional[_Union[FlowListChangedV2, _Mapping]] = ..., flow_reload_status: _Optional[_Union[FlowReloadStatusV2, _Mapping]] = ..., catalog_reload_required: _Optional[_Union[CatalogReloadRequiredV2, _Mapping]] = ..., cursor: _Optional[_Union[LifecycleCursorV2, _Mapping]] = ..., scope_ref: _Optional[_Union[ScopeReferenceV2, _Mapping]] = ...) -> None: ...
