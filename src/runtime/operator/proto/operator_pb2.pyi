from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StartRunRequest(_message.Message):
    __slots__ = ("flow_name", "input_json", "context_json", "input_files", "run_id", "workflow_selector")
    FLOW_NAME_FIELD_NUMBER: _ClassVar[int]
    INPUT_JSON_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_JSON_FIELD_NUMBER: _ClassVar[int]
    INPUT_FILES_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    flow_name: str
    input_json: str
    context_json: str
    input_files: _containers.RepeatedCompositeFieldContainer[FileAttachment]
    run_id: str
    workflow_selector: str
    def __init__(self, flow_name: _Optional[str] = ..., input_json: _Optional[str] = ..., context_json: _Optional[str] = ..., input_files: _Optional[_Iterable[_Union[FileAttachment, _Mapping]]] = ..., run_id: _Optional[str] = ..., workflow_selector: _Optional[str] = ...) -> None: ...

class FileAttachment(_message.Message):
    __slots__ = ("field_name", "name", "content", "content_type", "sha256")
    FIELD_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    field_name: str
    name: str
    content: bytes
    content_type: str
    sha256: str
    def __init__(self, field_name: _Optional[str] = ..., name: _Optional[str] = ..., content: _Optional[bytes] = ..., content_type: _Optional[str] = ..., sha256: _Optional[str] = ...) -> None: ...

class StartRunResponse(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class CancelRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class GetRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class ListRunSummariesRequest(_message.Message):
    __slots__ = ("workflow_selector", "page_size", "page_token")
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    workflow_selector: str
    page_size: int
    page_token: str
    def __init__(self, workflow_selector: _Optional[str] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class GetRunSnapshotRequest(_message.Message):
    __slots__ = ("run_id", "operator_instance_id", "as_of_sequence")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    operator_instance_id: str
    as_of_sequence: int
    def __init__(self, run_id: _Optional[str] = ..., operator_instance_id: _Optional[str] = ..., as_of_sequence: _Optional[int] = ...) -> None: ...

class ListLogsRequest(_message.Message):
    __slots__ = ("page_token", "after_sequence", "page_size")
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    page_token: str
    after_sequence: int
    page_size: int
    def __init__(self, page_token: _Optional[str] = ..., after_sequence: _Optional[int] = ..., page_size: _Optional[int] = ...) -> None: ...

class ListAgentEventsRequest(_message.Message):
    __slots__ = ("page_token", "after_event_sequence", "page_size")
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    AFTER_EVENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    page_token: str
    after_event_sequence: int
    page_size: int
    def __init__(self, page_token: _Optional[str] = ..., after_event_sequence: _Optional[int] = ..., page_size: _Optional[int] = ...) -> None: ...

class ReadTraceRequest(_message.Message):
    __slots__ = ("run_id", "node_id", "revision", "operator_instance_id")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    node_id: str
    revision: int
    operator_instance_id: str
    def __init__(self, run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., revision: _Optional[int] = ..., operator_instance_id: _Optional[str] = ...) -> None: ...

class ReadDetailRequest(_message.Message):
    __slots__ = ("body_token",)
    BODY_TOKEN_FIELD_NUMBER: _ClassVar[int]
    body_token: str
    def __init__(self, body_token: _Optional[str] = ...) -> None: ...

class StreamRunDeltasRequest(_message.Message):
    __slots__ = ("operator_instance_id", "after_sequence")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    after_sequence: int
    def __init__(self, operator_instance_id: _Optional[str] = ..., after_sequence: _Optional[int] = ...) -> None: ...

class NodeEdges(_message.Message):
    __slots__ = ("children",)
    CHILDREN_FIELD_NUMBER: _ClassVar[int]
    children: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, children: _Optional[_Iterable[str]] = ...) -> None: ...

class FlowInfoMsg(_message.Message):
    __slots__ = ("name", "file_path", "node_ids", "graph", "node_types", "display_names", "cron", "next_run_at", "last_run_at", "workflow_id", "display_name", "root_alias", "relative_file", "builder_symbol", "agent_node_ids", "agent_metadata_json", "webhook_path", "webhook_url", "webhook_active")
    class GraphEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: NodeEdges
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[NodeEdges, _Mapping]] = ...) -> None: ...
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
    class AgentMetadataJsonEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    GRAPH_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPES_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAMES_FIELD_NUMBER: _ClassVar[int]
    CRON_FIELD_NUMBER: _ClassVar[int]
    NEXT_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ROOT_ALIAS_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_FILE_FIELD_NUMBER: _ClassVar[int]
    BUILDER_SYMBOL_FIELD_NUMBER: _ClassVar[int]
    AGENT_NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    AGENT_METADATA_JSON_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_PATH_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_URL_FIELD_NUMBER: _ClassVar[int]
    WEBHOOK_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    name: str
    file_path: str
    node_ids: _containers.RepeatedScalarFieldContainer[str]
    graph: _containers.MessageMap[str, NodeEdges]
    node_types: _containers.ScalarMap[str, str]
    display_names: _containers.ScalarMap[str, str]
    cron: str
    next_run_at: float
    last_run_at: float
    workflow_id: str
    display_name: str
    root_alias: str
    relative_file: str
    builder_symbol: str
    agent_node_ids: _containers.RepeatedScalarFieldContainer[str]
    agent_metadata_json: _containers.ScalarMap[str, str]
    webhook_path: str
    webhook_url: str
    webhook_active: bool
    def __init__(self, name: _Optional[str] = ..., file_path: _Optional[str] = ..., node_ids: _Optional[_Iterable[str]] = ..., graph: _Optional[_Mapping[str, NodeEdges]] = ..., node_types: _Optional[_Mapping[str, str]] = ..., display_names: _Optional[_Mapping[str, str]] = ..., cron: _Optional[str] = ..., next_run_at: _Optional[float] = ..., last_run_at: _Optional[float] = ..., workflow_id: _Optional[str] = ..., display_name: _Optional[str] = ..., root_alias: _Optional[str] = ..., relative_file: _Optional[str] = ..., builder_symbol: _Optional[str] = ..., agent_node_ids: _Optional[_Iterable[str]] = ..., agent_metadata_json: _Optional[_Mapping[str, str]] = ..., webhook_path: _Optional[str] = ..., webhook_url: _Optional[str] = ..., webhook_active: bool = ...) -> None: ...

class FlowList(_message.Message):
    __slots__ = ("flows", "diagnostics")
    FLOWS_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    flows: _containers.RepeatedCompositeFieldContainer[FlowInfoMsg]
    diagnostics: _containers.RepeatedCompositeFieldContainer[DiscoveryDiagnosticMsg]
    def __init__(self, flows: _Optional[_Iterable[_Union[FlowInfoMsg, _Mapping]]] = ..., diagnostics: _Optional[_Iterable[_Union[DiscoveryDiagnosticMsg, _Mapping]]] = ...) -> None: ...

class DiscoveryDiagnosticMsg(_message.Message):
    __slots__ = ("path", "kind", "message")
    PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    path: str
    kind: str
    message: str
    def __init__(self, path: _Optional[str] = ..., kind: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class ResultFileAttachment(_message.Message):
    __slots__ = ("attachment_id", "name", "content", "media_type", "sha256")
    ATTACHMENT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    attachment_id: str
    name: str
    content: bytes
    media_type: str
    sha256: str
    def __init__(self, attachment_id: _Optional[str] = ..., name: _Optional[str] = ..., content: _Optional[bytes] = ..., media_type: _Optional[str] = ..., sha256: _Optional[str] = ...) -> None: ...

class RunResultMsg(_message.Message):
    __slots__ = ("value_json", "files")
    VALUE_JSON_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    value_json: str
    files: _containers.RepeatedCompositeFieldContainer[ResultFileAttachment]
    def __init__(self, value_json: _Optional[str] = ..., files: _Optional[_Iterable[_Union[ResultFileAttachment, _Mapping]]] = ...) -> None: ...

class RunSummaryMsg(_message.Message):
    __slots__ = ("run_id", "flow_name", "status", "started_at", "ended_at", "triggered_by", "workflow_id", "workflow_display_name", "created_sequence", "revision")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    FLOW_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    TRIGGERED_BY_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    flow_name: str
    status: str
    started_at: float
    ended_at: float
    triggered_by: str
    workflow_id: str
    workflow_display_name: str
    created_sequence: int
    revision: int
    def __init__(self, run_id: _Optional[str] = ..., flow_name: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., triggered_by: _Optional[str] = ..., workflow_id: _Optional[str] = ..., workflow_display_name: _Optional[str] = ..., created_sequence: _Optional[int] = ..., revision: _Optional[int] = ...) -> None: ...

class TraceDescriptorMsg(_message.Message):
    __slots__ = ("status", "revision", "available", "complete", "event_count", "size_bytes", "latest_event_sequence")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    EVENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    LATEST_EVENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    status: str
    revision: int
    available: bool
    complete: bool
    event_count: int
    size_bytes: int
    latest_event_sequence: int
    def __init__(self, status: _Optional[str] = ..., revision: _Optional[int] = ..., available: bool = ..., complete: bool = ..., event_count: _Optional[int] = ..., size_bytes: _Optional[int] = ..., latest_event_sequence: _Optional[int] = ...) -> None: ...

class NodeSnapshotMsg(_message.Message):
    __slots__ = ("node_id", "name", "node_type", "status", "started_at", "ended_at", "trace", "revision", "event_page_token")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    EVENT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    name: str
    node_type: str
    status: str
    started_at: float
    ended_at: float
    trace: TraceDescriptorMsg
    revision: int
    event_page_token: str
    def __init__(self, node_id: _Optional[str] = ..., name: _Optional[str] = ..., node_type: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., trace: _Optional[_Union[TraceDescriptorMsg, _Mapping]] = ..., revision: _Optional[int] = ..., event_page_token: _Optional[str] = ...) -> None: ...

class RunSnapshotMsg(_message.Message):
    __slots__ = ("operator_instance_id", "as_of_sequence", "summary", "nodes", "latest_log_sequence", "log_page_token")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    LATEST_LOG_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    LOG_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    as_of_sequence: int
    summary: RunSummaryMsg
    nodes: _containers.RepeatedCompositeFieldContainer[NodeSnapshotMsg]
    latest_log_sequence: int
    log_page_token: str
    def __init__(self, operator_instance_id: _Optional[str] = ..., as_of_sequence: _Optional[int] = ..., summary: _Optional[_Union[RunSummaryMsg, _Mapping]] = ..., nodes: _Optional[_Iterable[_Union[NodeSnapshotMsg, _Mapping]]] = ..., latest_log_sequence: _Optional[int] = ..., log_page_token: _Optional[str] = ...) -> None: ...

class RunSummaryPage(_message.Message):
    __slots__ = ("operator_instance_id", "as_of_sequence", "runs", "next_page_token")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RUNS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    as_of_sequence: int
    runs: _containers.RepeatedCompositeFieldContainer[RunSummaryMsg]
    next_page_token: str
    def __init__(self, operator_instance_id: _Optional[str] = ..., as_of_sequence: _Optional[int] = ..., runs: _Optional[_Iterable[_Union[RunSummaryMsg, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class LogRecordDescriptorMsg(_message.Message):
    __slots__ = ("sequence", "timestamp", "level", "node_id", "size_bytes", "body_token")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    BODY_TOKEN_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    timestamp: float
    level: str
    node_id: str
    size_bytes: int
    body_token: str
    def __init__(self, sequence: _Optional[int] = ..., timestamp: _Optional[float] = ..., level: _Optional[str] = ..., node_id: _Optional[str] = ..., size_bytes: _Optional[int] = ..., body_token: _Optional[str] = ...) -> None: ...

class LogPage(_message.Message):
    __slots__ = ("operator_instance_id", "as_of_sequence", "logs", "next_page_token")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    as_of_sequence: int
    logs: _containers.RepeatedCompositeFieldContainer[LogRecordDescriptorMsg]
    next_page_token: str
    def __init__(self, operator_instance_id: _Optional[str] = ..., as_of_sequence: _Optional[int] = ..., logs: _Optional[_Iterable[_Union[LogRecordDescriptorMsg, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class AgentEventDescriptorMsg(_message.Message):
    __slots__ = ("event_sequence", "size_bytes", "body_token")
    EVENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    BODY_TOKEN_FIELD_NUMBER: _ClassVar[int]
    event_sequence: int
    size_bytes: int
    body_token: str
    def __init__(self, event_sequence: _Optional[int] = ..., size_bytes: _Optional[int] = ..., body_token: _Optional[str] = ...) -> None: ...

class AgentEventPage(_message.Message):
    __slots__ = ("operator_instance_id", "as_of_sequence", "run_id", "node_id", "events", "next_page_token")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    as_of_sequence: int
    run_id: str
    node_id: str
    events: _containers.RepeatedCompositeFieldContainer[AgentEventDescriptorMsg]
    next_page_token: str
    def __init__(self, operator_instance_id: _Optional[str] = ..., as_of_sequence: _Optional[int] = ..., run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., events: _Optional[_Iterable[_Union[AgentEventDescriptorMsg, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class TraceChunk(_message.Message):
    __slots__ = ("revision", "chunk_index", "data", "eof")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CHUNK_INDEX_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EOF_FIELD_NUMBER: _ClassVar[int]
    revision: int
    chunk_index: int
    data: bytes
    eof: bool
    def __init__(self, revision: _Optional[int] = ..., chunk_index: _Optional[int] = ..., data: _Optional[bytes] = ..., eof: bool = ...) -> None: ...

class DetailChunk(_message.Message):
    __slots__ = ("chunk_index", "data", "eof")
    CHUNK_INDEX_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EOF_FIELD_NUMBER: _ClassVar[int]
    chunk_index: int
    data: bytes
    eof: bool
    def __init__(self, chunk_index: _Optional[int] = ..., data: _Optional[bytes] = ..., eof: bool = ...) -> None: ...

class RunCreatedDelta(_message.Message):
    __slots__ = ("summary", "nodes")
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    summary: RunSummaryMsg
    nodes: _containers.RepeatedCompositeFieldContainer[NodeSnapshotMsg]
    def __init__(self, summary: _Optional[_Union[RunSummaryMsg, _Mapping]] = ..., nodes: _Optional[_Iterable[_Union[NodeSnapshotMsg, _Mapping]]] = ...) -> None: ...

class RunStatusChangedDelta(_message.Message):
    __slots__ = ("run_id", "status", "started_at", "ended_at", "revision")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    status: str
    started_at: float
    ended_at: float
    revision: int
    def __init__(self, run_id: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., revision: _Optional[int] = ...) -> None: ...

class NodeStatusChangedDelta(_message.Message):
    __slots__ = ("run_id", "node_id", "status", "started_at", "ended_at", "revision")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    node_id: str
    status: str
    started_at: float
    ended_at: float
    revision: int
    def __init__(self, run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., revision: _Optional[int] = ...) -> None: ...

class LogAppendedDelta(_message.Message):
    __slots__ = ("run_id", "log")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    LOG_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    log: LogRecordDescriptorMsg
    def __init__(self, run_id: _Optional[str] = ..., log: _Optional[_Union[LogRecordDescriptorMsg, _Mapping]] = ...) -> None: ...

class AgentEventAppendedDelta(_message.Message):
    __slots__ = ("run_id", "node_id", "event")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    node_id: str
    event: AgentEventDescriptorMsg
    def __init__(self, run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., event: _Optional[_Union[AgentEventDescriptorMsg, _Mapping]] = ...) -> None: ...

class TraceFinalizedDelta(_message.Message):
    __slots__ = ("run_id", "node_id", "trace")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    node_id: str
    trace: TraceDescriptorMsg
    def __init__(self, run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., trace: _Optional[_Union[TraceDescriptorMsg, _Mapping]] = ...) -> None: ...

class RunDelta(_message.Message):
    __slots__ = ("sequence", "run_created", "run_status_changed", "node_status_changed", "log_appended", "agent_event_appended", "trace_finalized")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RUN_CREATED_FIELD_NUMBER: _ClassVar[int]
    RUN_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    NODE_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    LOG_APPENDED_FIELD_NUMBER: _ClassVar[int]
    AGENT_EVENT_APPENDED_FIELD_NUMBER: _ClassVar[int]
    TRACE_FINALIZED_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    run_created: RunCreatedDelta
    run_status_changed: RunStatusChangedDelta
    node_status_changed: NodeStatusChangedDelta
    log_appended: LogAppendedDelta
    agent_event_appended: AgentEventAppendedDelta
    trace_finalized: TraceFinalizedDelta
    def __init__(self, sequence: _Optional[int] = ..., run_created: _Optional[_Union[RunCreatedDelta, _Mapping]] = ..., run_status_changed: _Optional[_Union[RunStatusChangedDelta, _Mapping]] = ..., node_status_changed: _Optional[_Union[NodeStatusChangedDelta, _Mapping]] = ..., log_appended: _Optional[_Union[LogAppendedDelta, _Mapping]] = ..., agent_event_appended: _Optional[_Union[AgentEventAppendedDelta, _Mapping]] = ..., trace_finalized: _Optional[_Union[TraceFinalizedDelta, _Mapping]] = ...) -> None: ...

class ResetRequired(_message.Message):
    __slots__ = ("history_floor", "latest_sequence")
    HISTORY_FLOOR_FIELD_NUMBER: _ClassVar[int]
    LATEST_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    history_floor: int
    latest_sequence: int
    def __init__(self, history_floor: _Optional[int] = ..., latest_sequence: _Optional[int] = ...) -> None: ...

class RunDeltaEnvelope(_message.Message):
    __slots__ = ("operator_instance_id", "delta", "reset_required")
    OPERATOR_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    RESET_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    operator_instance_id: str
    delta: RunDelta
    reset_required: ResetRequired
    def __init__(self, operator_instance_id: _Optional[str] = ..., delta: _Optional[_Union[RunDelta, _Mapping]] = ..., reset_required: _Optional[_Union[ResetRequired, _Mapping]] = ...) -> None: ...
