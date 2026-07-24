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

class ListRunsRequest(_message.Message):
    __slots__ = ("flow_name", "workflow_selector")
    FLOW_NAME_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    flow_name: str
    workflow_selector: str
    def __init__(self, flow_name: _Optional[str] = ..., workflow_selector: _Optional[str] = ...) -> None: ...

class GetRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class StreamRequest(_message.Message):
    __slots__ = ("since_sequence",)
    SINCE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    since_sequence: int
    def __init__(self, since_sequence: _Optional[int] = ...) -> None: ...

class NodeEdges(_message.Message):
    __slots__ = ("children",)
    CHILDREN_FIELD_NUMBER: _ClassVar[int]
    children: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, children: _Optional[_Iterable[str]] = ...) -> None: ...

class FlowInfoMsg(_message.Message):
    __slots__ = ("name", "file_path", "node_ids", "graph", "node_types", "display_names", "cron", "next_run_at", "last_run_at", "workflow_id", "display_name", "root_alias", "relative_file", "builder_symbol")
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
    def __init__(self, name: _Optional[str] = ..., file_path: _Optional[str] = ..., node_ids: _Optional[_Iterable[str]] = ..., graph: _Optional[_Mapping[str, NodeEdges]] = ..., node_types: _Optional[_Mapping[str, str]] = ..., display_names: _Optional[_Mapping[str, str]] = ..., cron: _Optional[str] = ..., next_run_at: _Optional[float] = ..., last_run_at: _Optional[float] = ..., workflow_id: _Optional[str] = ..., display_name: _Optional[str] = ..., root_alias: _Optional[str] = ..., relative_file: _Optional[str] = ..., builder_symbol: _Optional[str] = ...) -> None: ...

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

class NodeStateMsg(_message.Message):
    __slots__ = ("node_id", "name", "node_type", "status", "started_at", "ended_at")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NODE_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    name: str
    node_type: str
    status: str
    started_at: float
    ended_at: float
    def __init__(self, node_id: _Optional[str] = ..., name: _Optional[str] = ..., node_type: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ...) -> None: ...

class LogEntryMsg(_message.Message):
    __slots__ = ("timestamp", "level", "node_id", "message")
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    timestamp: float
    level: str
    node_id: str
    message: str
    def __init__(self, timestamp: _Optional[float] = ..., level: _Optional[str] = ..., node_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class RunStateMsg(_message.Message):
    __slots__ = ("run_id", "flow_name", "status", "started_at", "ended_at", "nodes", "logs", "triggered_by", "workflow_id", "workflow_display_name")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    FLOW_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    TRIGGERED_BY_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    flow_name: str
    status: str
    started_at: float
    ended_at: float
    nodes: _containers.RepeatedCompositeFieldContainer[NodeStateMsg]
    logs: _containers.RepeatedCompositeFieldContainer[LogEntryMsg]
    triggered_by: str
    workflow_id: str
    workflow_display_name: str
    def __init__(self, run_id: _Optional[str] = ..., flow_name: _Optional[str] = ..., status: _Optional[str] = ..., started_at: _Optional[float] = ..., ended_at: _Optional[float] = ..., nodes: _Optional[_Iterable[_Union[NodeStateMsg, _Mapping]]] = ..., logs: _Optional[_Iterable[_Union[LogEntryMsg, _Mapping]]] = ..., triggered_by: _Optional[str] = ..., workflow_id: _Optional[str] = ..., workflow_display_name: _Optional[str] = ...) -> None: ...

class RunList(_message.Message):
    __slots__ = ("runs",)
    RUNS_FIELD_NUMBER: _ClassVar[int]
    runs: _containers.RepeatedCompositeFieldContainer[RunStateMsg]
    def __init__(self, runs: _Optional[_Iterable[_Union[RunStateMsg, _Mapping]]] = ...) -> None: ...

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

class RunUpdate(_message.Message):
    __slots__ = ("sequence", "run")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RUN_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    run: RunStateMsg
    def __init__(self, sequence: _Optional[int] = ..., run: _Optional[_Union[RunStateMsg, _Mapping]] = ...) -> None: ...
