/**
 * Hand-written compatibility view model for the web UI.
 *
 * These stable UI shapes are intentionally separate from the generated
 * OperatorServiceV2 messages. The companion `create(partial?)` factories fill
 * the same defaults the old UI fixtures used, so components and tests do not
 * depend on generated transport messages or the protobuf runtime.
 */

/** Deep partial matching protobuf-ts PartialMessage semantics. */
export type DeepPartial<T> = T extends Uint8Array
  ? T
  : T extends Array<infer U>
    ? Array<DeepPartial<U>>
    : T extends object
      ? { [K in keyof T]?: DeepPartial<T[K]> }
      : T;

export enum DescriptorPageOrder {
  /**
   * @generated from protobuf enum value: DESCRIPTOR_PAGE_ORDER_FORWARD = 0;
   */
  FORWARD = 0,
  /**
   * @generated from protobuf enum value: DESCRIPTOR_PAGE_ORDER_NEWEST_FIRST = 1;
   */
  NEWEST_FIRST = 1,
}

export interface NodeEdges {
  /**
   * @generated from protobuf field: repeated string children = 1
   */
  children: string[];
}

export const NodeEdges = {
  create(value: DeepPartial<NodeEdges> = {}): NodeEdges {
    return { children: [], ...value } as NodeEdges;
  },
};

export interface ScanTargetMsg {
  /**
   * @generated from protobuf field: string alias = 1
   */
  alias: string;
  /**
   * @generated from protobuf field: string target_path = 2
   */
  targetPath: string;
  /**
   * @generated from protobuf field: string kind = 3
   */
  kind: string;
}

export const ScanTargetMsg = {
  create(value: DeepPartial<ScanTargetMsg> = {}): ScanTargetMsg {
    return { alias: "", targetPath: "", kind: "", ...value } as ScanTargetMsg;
  },
};

export interface DiscoveryDiagnosticMsg {
  /**
   * @generated from protobuf field: string path = 1
   */
  path: string;
  /**
   * @generated from protobuf field: string kind = 2
   */
  kind: string;
  /**
   * @generated from protobuf field: string message = 3
   */
  message: string;
}

export const DiscoveryDiagnosticMsg = {
  create(value: DeepPartial<DiscoveryDiagnosticMsg> = {}): DiscoveryDiagnosticMsg {
    return { path: "", kind: "", message: "", ...value } as DiscoveryDiagnosticMsg;
  },
};

export interface FlowInfoMsg {
  /**
   * @generated from protobuf field: string name = 1
   */
  name: string;
  /**
   * @generated from protobuf field: string file_path = 2
   */
  filePath: string;
  /**
   * @generated from protobuf field: repeated string node_ids = 3
   */
  nodeIds: string[];
  /**
   * @generated from protobuf field: map<string, avalanche.operator.NodeEdges> graph = 4
   */
  graph: {
    [key: string]: NodeEdges;
  };
  /**
   * @generated from protobuf field: map<string, string> node_types = 5
   */
  nodeTypes: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: map<string, string> display_names = 6
   */
  displayNames: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: string cron = 7
   */
  cron: string;
  /**
   * @generated from protobuf field: double next_run_at = 8
   */
  nextRunAt: number;
  /**
   * @generated from protobuf field: double last_run_at = 9
   */
  lastRunAt: number;
  /**
   * @generated from protobuf field: string workflow_id = 10
   */
  workflowId: string;
  /**
   * @generated from protobuf field: string display_name = 11
   */
  displayName: string;
  /**
   * @generated from protobuf field: string root_alias = 12
   */
  rootAlias: string;
  /**
   * @generated from protobuf field: string relative_file = 13
   */
  relativeFile: string;
  /**
   * @generated from protobuf field: string builder_symbol = 14
   */
  builderSymbol: string;
  /**
   * @generated from protobuf field: repeated string agent_node_ids = 15
   */
  agentNodeIds: string[];
  /**
   * @generated from protobuf field: map<string, string> agent_metadata_json = 16
   */
  agentMetadataJson: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: string webhook_path = 17
   */
  webhookPath: string;
  /**
   * @generated from protobuf field: string webhook_url = 18
   */
  webhookUrl: string;
  /**
   * @generated from protobuf field: bool webhook_active = 19
   */
  webhookActive: boolean;
}

export const FlowInfoMsg = {
  create(value: DeepPartial<FlowInfoMsg> = {}): FlowInfoMsg {
    return {
      name: "",
      filePath: "",
      nodeIds: [],
      graph: {},
      nodeTypes: {},
      displayNames: {},
      cron: "",
      nextRunAt: 0,
      lastRunAt: 0,
      workflowId: "",
      displayName: "",
      rootAlias: "",
      relativeFile: "",
      builderSymbol: "",
      agentNodeIds: [],
      agentMetadataJson: {},
      webhookPath: "",
      webhookUrl: "",
      webhookActive: false,
      ...value,
    } as FlowInfoMsg;
  },
};

export interface CatalogSnapshotMsg {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * Server-issued event ULID for the catalog baseline.
   */
  asOfEventUlid: string;
  /**
   * @generated from protobuf field: uint64 revision = 3
   */
  revision: string;
  /**
   * @generated from protobuf field: repeated avalanche.operator.FlowInfoMsg workflows = 4
   */
  workflows: FlowInfoMsg[];
  /**
   * @generated from protobuf field: repeated avalanche.operator.ScanTargetMsg scan_targets = 5
   */
  scanTargets: ScanTargetMsg[];
  /**
   * @generated from protobuf field: repeated avalanche.operator.DiscoveryDiagnosticMsg diagnostics = 6
   */
  diagnostics: DiscoveryDiagnosticMsg[];
}

export const CatalogSnapshotMsg = {
  create(value: DeepPartial<CatalogSnapshotMsg> = {}): CatalogSnapshotMsg {
    return {
      operatorInstanceId: "",
      asOfEventUlid: "",
      revision: "0",
      workflows: [],
      scanTargets: [],
      diagnostics: [],
      ...value,
    } as CatalogSnapshotMsg;
  },
};

export interface RunSummaryMsg {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: string flow_name = 2
   */
  flowName: string;
  /**
   * @generated from protobuf field: string status = 3
   */
  status: string;
  /**
   * @generated from protobuf field: double started_at = 4
   */
  startedAt: number;
  /**
   * @generated from protobuf field: double ended_at = 5
   */
  endedAt: number;
  /**
   * @generated from protobuf field: string triggered_by = 6
   */
  triggeredBy: string;
  /**
   * @generated from protobuf field: string workflow_id = 7
   */
  workflowId: string;
  /**
   * @generated from protobuf field: string workflow_display_name = 8
   */
  workflowDisplayName: string;
  /**
   * @generated from protobuf field: uint64 created_sequence = 9
   */
  createdSequence: string;
  /**
   * @generated from protobuf field: uint64 revision = 10
   */
  revision: string;
  /**
   * @generated from protobuf field: double triggered_at = 11
   */
  triggeredAt: number;
}

export const RunSummaryMsg = {
  create(value: DeepPartial<RunSummaryMsg> = {}): RunSummaryMsg {
    return {
      runId: "",
      flowName: "",
      status: "",
      startedAt: 0,
      endedAt: 0,
      triggeredBy: "",
      workflowId: "",
      workflowDisplayName: "",
      createdSequence: "0",
      revision: "0",
      triggeredAt: 0,
      ...value,
    } as RunSummaryMsg;
  },
};

export interface NodeSnapshotMsg {
  /**
   * @generated from protobuf field: string node_id = 1
   */
  nodeId: string;
  /**
   * @generated from protobuf field: string name = 2
   */
  name: string;
  /**
   * @generated from protobuf field: string node_type = 3
   */
  nodeType: string;
  /**
   * @generated from protobuf field: string status = 4
   */
  status: string;
  /**
   * @generated from protobuf field: double started_at = 5
   */
  startedAt: number;
  /**
   * @generated from protobuf field: double ended_at = 6
   */
  endedAt: number;
  /**
   * @generated from protobuf field: avalanche.operator.TraceDescriptorMsg trace = 7
   */
  trace?: TraceDescriptorMsg;
  /**
   * @generated from protobuf field: uint64 revision = 8
   */
  revision: string;
  /**
   * @generated from protobuf field: string event_page_token = 9
   */
  eventPageToken: string;
  /**
   * @generated from protobuf field: optional string error = 10
   */
  error?: string;
  /**
   * @generated from protobuf field: optional double running_elapsed_seconds = 11
   */
  runningElapsedSeconds?: number;
}

export const NodeSnapshotMsg = {
  create(value: DeepPartial<NodeSnapshotMsg> = {}): NodeSnapshotMsg {
    return {
      nodeId: "",
      name: "",
      nodeType: "",
      status: "",
      startedAt: 0,
      endedAt: 0,
      trace: undefined,
      revision: "0",
      eventPageToken: "",
      error: undefined,
      runningElapsedSeconds: undefined,
      ...value,
    } as NodeSnapshotMsg;
  },
};

export interface TraceHeaderMsg {
  /**
   * @generated from protobuf field: string status = 1
   */
  status: string;
  /**
   * @generated from protobuf field: string model = 2
   */
  model: string;
  /**
   * @generated from protobuf field: optional string sub_model = 3
   */
  subModel?: string;
  /**
   * @generated from protobuf field: uint64 iterations = 4
   */
  iterations: string;
  /**
   * @generated from protobuf field: uint64 max_iterations = 5
   */
  maxIterations: string;
  /**
   * @generated from protobuf field: uint64 duration_ms = 6
   */
  durationMs: string;
  /**
   * @generated from protobuf field: string usage_json = 7
   */
  usageJson: string;
  /**
   * @generated from protobuf field: optional string telemetry_json = 8
   */
  telemetryJson?: string;
}

export const TraceHeaderMsg = {
  create(value: DeepPartial<TraceHeaderMsg> = {}): TraceHeaderMsg {
    return {
      status: "",
      model: "",
      subModel: undefined,
      iterations: "0",
      maxIterations: "0",
      durationMs: "0",
      usageJson: "",
      telemetryJson: undefined,
      ...value,
    } as TraceHeaderMsg;
  },
};

export interface TraceDescriptorMsg {
  /**
   * @generated from protobuf field: string status = 1
   */
  status: string;
  /**
   * @generated from protobuf field: uint64 revision = 2
   */
  revision: string;
  /**
   * @generated from protobuf field: bool available = 3
   */
  available: boolean;
  /**
   * @generated from protobuf field: bool complete = 4
   */
  complete: boolean;
  /**
   * @generated from protobuf field: uint64 event_count = 5
   */
  eventCount: string;
  /**
   * @generated from protobuf field: uint64 size_bytes = 6
   */
  sizeBytes: string;
  /**
   * @generated from protobuf field: uint64 latest_event_sequence = 7
   */
  latestEventSequence: string;
  /**
   * @generated from protobuf field: avalanche.operator.TraceHeaderMsg header = 8
   */
  header?: TraceHeaderMsg;
}

export const TraceDescriptorMsg = {
  create(value: DeepPartial<TraceDescriptorMsg> = {}): TraceDescriptorMsg {
    return {
      status: "",
      revision: "0",
      available: false,
      complete: false,
      eventCount: "0",
      sizeBytes: "0",
      latestEventSequence: "0",
      header: undefined,
      ...value,
    } as TraceDescriptorMsg;
  },
};

export type TerminalSealStatus = "success" | "failed" | "cancelled";

export interface TerminalSealMsg {
  /**
   * Existing activity descriptor identity.
   */
  activityId: string;
  /**
   * Per-run activity ordering, independent of the lifecycle event cursor.
   */
  runSequence: string;
  /**
   * Existing activity descriptor timestamp.
   */
  timestamp: number;
  /**
   * @generated from protobuf field: string terminal_status = 1
   */
  terminalStatus: TerminalSealStatus;
  /**
   * @generated from protobuf field: optional string reason = 2
   */
  reason?: string;
}

export const TerminalSealMsg = {
  create(value: DeepPartial<TerminalSealMsg> = {}): TerminalSealMsg {
    return {
      activityId: "",
      runSequence: "0",
      timestamp: 0,
      terminalStatus: "success",
      reason: undefined,
      ...value,
    } as TerminalSealMsg;
  },
};

export interface WorkflowTopologyMsg {
  /**
   * @generated from protobuf field: repeated string node_ids = 1
   */
  nodeIds: string[];
  /**
   * @generated from protobuf field: map<string, avalanche.operator.NodeEdges> graph = 2
   */
  graph: {
    [key: string]: NodeEdges;
  };
  /**
   * @generated from protobuf field: map<string, string> node_types = 3
   */
  nodeTypes: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: map<string, string> display_names = 4
   */
  displayNames: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: map<string, string> agent_field_schemas_json = 5
   */
  agentFieldSchemasJson: {
    [key: string]: string;
  };
  /**
   * @generated from protobuf field: map<string, string> agent_instruction_lines = 6
   */
  agentInstructionLines: {
    [key: string]: string;
  };
}

export const WorkflowTopologyMsg = {
  create(value: DeepPartial<WorkflowTopologyMsg> = {}): WorkflowTopologyMsg {
    return {
      nodeIds: [],
      graph: {},
      nodeTypes: {},
      displayNames: {},
      agentFieldSchemasJson: {},
      agentInstructionLines: {},
      ...value,
    } as WorkflowTopologyMsg;
  },
};

export interface RunSnapshotMsg {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * Server-issued event ULID for the run baseline.
   */
  asOfEventUlid: string;
  /**
   * @generated from protobuf field: avalanche.operator.RunSummaryMsg summary = 3
   */
  summary?: RunSummaryMsg;
  /**
   * @generated from protobuf field: repeated avalanche.operator.NodeSnapshotMsg nodes = 4
   */
  nodes: NodeSnapshotMsg[];
  /**
   * @generated from protobuf field: uint64 latest_log_sequence = 5
   */
  latestLogSequence: string;
  /**
   * @generated from protobuf field: string log_page_token = 6
   */
  logPageToken: string;
  /**
   * @generated from protobuf field: avalanche.operator.WorkflowTopologyMsg topology = 7
   */
  topology?: WorkflowTopologyMsg;
  /**
   * Exact terminal activity descriptor mirrored by the server snapshot.
   */
  terminalSeal?: TerminalSealMsg;
}

export const RunSnapshotMsg = {
  create(value: DeepPartial<RunSnapshotMsg> = {}): RunSnapshotMsg {
    return {
      operatorInstanceId: "",
      asOfEventUlid: "",
      summary: undefined,
      nodes: [],
      latestLogSequence: "0",
      logPageToken: "",
      topology: undefined,
      terminalSeal: undefined,
      ...value,
    } as RunSnapshotMsg;
  },
};

export interface LogRecordDescriptorMsg {
  /**
   * @generated from protobuf field: uint64 sequence = 1
   */
  sequence: string;
  /**
   * @generated from protobuf field: double timestamp = 2
   */
  timestamp: number;
  /**
   * @generated from protobuf field: string level = 3
   */
  level: string;
  /**
   * @generated from protobuf field: string node_id = 4
   */
  nodeId: string;
  /**
   * @generated from protobuf field: uint64 size_bytes = 5
   */
  sizeBytes: string;
  /**
   * @generated from protobuf field: string body_token = 6
   */
  bodyToken: string;
}

export const LogRecordDescriptorMsg = {
  create(value: DeepPartial<LogRecordDescriptorMsg> = {}): LogRecordDescriptorMsg {
    return {
      sequence: "0",
      timestamp: 0,
      level: "",
      nodeId: "",
      sizeBytes: "0",
      bodyToken: "",
      ...value,
    } as LogRecordDescriptorMsg;
  },
};

export interface AgentEventDescriptorMsg {
  /**
   * @generated from protobuf field: uint64 event_sequence = 1
   */
  eventSequence: string;
  /**
   * @generated from protobuf field: uint64 size_bytes = 2
   */
  sizeBytes: string;
  /**
   * @generated from protobuf field: string body_token = 3
   */
  bodyToken: string;
  /**
   * @generated from protobuf field: string invocation_id = 4
   */
  invocationId: string;
  /**
   * @generated from protobuf field: string event_kind = 5
   */
  eventKind: string;
  /**
   * @generated from protobuf field: optional uint32 iteration = 6
   */
  iteration?: number;
  /**
   * @generated from protobuf field: optional uint64 duration_ms = 7
   */
  durationMs?: string;
  /**
   * @generated from protobuf field: bool error = 8
   */
  error: boolean;
  /**
   * @generated from protobuf field: uint32 tool_count = 9
   */
  toolCount: number;
  /**
   * @generated from protobuf field: uint32 predict_count = 10
   */
  predictCount: number;
}

export const AgentEventDescriptorMsg = {
  create(value: DeepPartial<AgentEventDescriptorMsg> = {}): AgentEventDescriptorMsg {
    return {
      eventSequence: "0",
      sizeBytes: "0",
      bodyToken: "",
      invocationId: "",
      eventKind: "",
      iteration: undefined,
      durationMs: undefined,
      error: false,
      toolCount: 0,
      predictCount: 0,
      ...value,
    } as AgentEventDescriptorMsg;
  },
};

export interface LogPage {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * Server-issued event ULID for the activity-page baseline.
   */
  asOfEventUlid: string;
  /**
   * @generated from protobuf field: repeated avalanche.operator.LogRecordDescriptorMsg logs = 3
   */
  logs: LogRecordDescriptorMsg[];
  /**
   * @generated from protobuf field: string next_page_token = 4
   */
  nextPageToken: string;
}

export const LogPage = {
  create(value: DeepPartial<LogPage> = {}): LogPage {
    return {
      operatorInstanceId: "",
      asOfEventUlid: "",
      logs: [],
      nextPageToken: "",
      ...value,
    } as LogPage;
  },
};

export interface AgentEventPage {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * Server-issued event ULID for the activity-page baseline.
   */
  asOfEventUlid: string;
  /**
   * @generated from protobuf field: string run_id = 3
   */
  runId: string;
  /**
   * @generated from protobuf field: string node_id = 4
   */
  nodeId: string;
  /**
   * @generated from protobuf field: repeated avalanche.operator.AgentEventDescriptorMsg events = 5
   */
  events: AgentEventDescriptorMsg[];
  /**
   * @generated from protobuf field: string next_page_token = 6
   */
  nextPageToken: string;
}

export const AgentEventPage = {
  create(value: DeepPartial<AgentEventPage> = {}): AgentEventPage {
    return {
      operatorInstanceId: "",
      asOfEventUlid: "",
      runId: "",
      nodeId: "",
      events: [],
      nextPageToken: "",
      ...value,
    } as AgentEventPage;
  },
};

export interface TraceChunk {
  /**
   * @generated from protobuf field: uint64 revision = 1
   */
  revision: string;
  /**
   * @generated from protobuf field: uint64 chunk_index = 2
   */
  chunkIndex: string;
  /**
   * @generated from protobuf field: bytes data = 3
   */
  data: Uint8Array;
  /**
   * @generated from protobuf field: bool eof = 4
   */
  eof: boolean;
}

export const TraceChunk = {
  create(value: DeepPartial<TraceChunk> = {}): TraceChunk {
    return {
      revision: "0",
      chunkIndex: "0",
      data: new Uint8Array(0),
      eof: false,
      ...value,
    } as TraceChunk;
  },
};

export interface DetailChunk {
  /**
   * @generated from protobuf field: uint64 chunk_index = 1
   */
  chunkIndex: string;
  /**
   * @generated from protobuf field: bytes data = 2
   */
  data: Uint8Array;
  /**
   * @generated from protobuf field: bool eof = 3
   */
  eof: boolean;
}

export const DetailChunk = {
  create(value: DeepPartial<DetailChunk> = {}): DetailChunk {
    return { chunkIndex: "0", data: new Uint8Array(0), eof: false, ...value } as DetailChunk;
  },
};

export interface RunSummaryPage {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * Server-issued event ULID for the run-summary-page baseline.
   */
  asOfEventUlid: string;
  /**
   * @generated from protobuf field: repeated avalanche.operator.RunSummaryMsg runs = 3
   */
  runs: RunSummaryMsg[];
  /**
   * @generated from protobuf field: string next_page_token = 4
   */
  nextPageToken: string;
}

export const RunSummaryPage = {
  create(value: DeepPartial<RunSummaryPage> = {}): RunSummaryPage {
    return {
      operatorInstanceId: "",
      asOfEventUlid: "",
      runs: [],
      nextPageToken: "",
      ...value,
    } as RunSummaryPage;
  },
};

export interface RunCreated {
  /**
   * @generated from protobuf field: avalanche.operator.RunSummaryMsg summary = 1
   */
  summary?: RunSummaryMsg;
  /**
   * @generated from protobuf field: repeated avalanche.operator.NodeSnapshotMsg nodes = 2
   */
  nodes: NodeSnapshotMsg[];
  /**
   * @generated from protobuf field: avalanche.operator.WorkflowTopologyMsg topology = 3
   */
  topology?: WorkflowTopologyMsg;
}

export const RunCreated = {
  create(value: DeepPartial<RunCreated> = {}): RunCreated {
    return { summary: undefined, nodes: [], topology: undefined, ...value } as RunCreated;
  },
};

export interface RunStatusChanged {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: string status = 2
   */
  status: string;
  /**
   * @generated from protobuf field: double started_at = 3
   */
  startedAt: number;
  /**
   * @generated from protobuf field: double ended_at = 4
   */
  endedAt: number;
  /**
   * @generated from protobuf field: uint64 revision = 5
   */
  revision: string;
}

export const RunStatusChanged = {
  create(value: DeepPartial<RunStatusChanged> = {}): RunStatusChanged {
    return {
      runId: "",
      status: "",
      startedAt: 0,
      endedAt: 0,
      revision: "0",
      ...value,
    } as RunStatusChanged;
  },
};

export interface NodeStatusChanged {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: string node_id = 2
   */
  nodeId: string;
  /**
   * @generated from protobuf field: string status = 3
   */
  status: string;
  /**
   * @generated from protobuf field: double started_at = 4
   */
  startedAt: number;
  /**
   * @generated from protobuf field: double ended_at = 5
   */
  endedAt: number;
  /**
   * @generated from protobuf field: uint64 revision = 6
   */
  revision: string;
  /**
   * @generated from protobuf field: optional string error = 7
   */
  error?: string;
  /**
   * @generated from protobuf field: optional double running_elapsed_seconds = 8
   */
  runningElapsedSeconds?: number;
}

export const NodeStatusChanged = {
  create(value: DeepPartial<NodeStatusChanged> = {}): NodeStatusChanged {
    return {
      runId: "",
      nodeId: "",
      status: "",
      startedAt: 0,
      endedAt: 0,
      revision: "0",
      error: undefined,
      runningElapsedSeconds: undefined,
      ...value,
    } as NodeStatusChanged;
  },
};

export interface LogAppended {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: avalanche.operator.LogRecordDescriptorMsg log = 2
   */
  log?: LogRecordDescriptorMsg;
}

export const LogAppended = {
  create(value: DeepPartial<LogAppended> = {}): LogAppended {
    return { runId: "", log: undefined, ...value } as LogAppended;
  },
};

export interface AgentEventAppended {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: string node_id = 2
   */
  nodeId: string;
  /**
   * @generated from protobuf field: avalanche.operator.AgentEventDescriptorMsg event = 3
   */
  event?: AgentEventDescriptorMsg;
}

export const AgentEventAppended = {
  create(value: DeepPartial<AgentEventAppended> = {}): AgentEventAppended {
    return { runId: "", nodeId: "", event: undefined, ...value } as AgentEventAppended;
  },
};

export interface TraceFinalized {
  /**
   * @generated from protobuf field: string run_id = 1
   */
  runId: string;
  /**
   * @generated from protobuf field: string node_id = 2
   */
  nodeId: string;
  /**
   * @generated from protobuf field: avalanche.operator.TraceDescriptorMsg trace = 3
   */
  trace?: TraceDescriptorMsg;
}

export const TraceFinalized = {
  create(value: DeepPartial<TraceFinalized> = {}): TraceFinalized {
    return { runId: "", nodeId: "", trace: undefined, ...value } as TraceFinalized;
  },
};

export interface TerminalSealAppended {
  runId: string;
  terminalSeal?: TerminalSealMsg;
}

export const TerminalSealAppended = {
  create(value: DeepPartial<TerminalSealAppended> = {}): TerminalSealAppended {
    return { runId: "", terminalSeal: undefined, ...value } as TerminalSealAppended;
  },
};

export interface CatalogReplaced {
  /**
   * @generated from protobuf field: avalanche.operator.CatalogSnapshotMsg catalog = 1
   */
  catalog?: CatalogSnapshotMsg;
}

export const CatalogReplaced = {
  create(value: DeepPartial<CatalogReplaced> = {}): CatalogReplaced {
    return { catalog: undefined, ...value } as CatalogReplaced;
  },
};

export interface CatalogReloadRequired {
  deploymentId: string;
}

export const CatalogReloadRequired = {
  create(value: DeepPartial<CatalogReloadRequired> = {}): CatalogReloadRequired {
    return { deploymentId: "", ...value } as CatalogReloadRequired;
  },
};

export interface WorkflowReloadStatus {
  /**
   * @generated from protobuf field: bool reloading = 1
   */
  reloading: boolean;
}

export const WorkflowReloadStatus = {
  create(value: DeepPartial<WorkflowReloadStatus> = {}): WorkflowReloadStatus {
    return { reloading: false, ...value } as WorkflowReloadStatus;
  },
};

export interface ResetRequired {
  /**
   * Server-issued lower event cursor bound.
   */
  historyFloorEventUlid: string;
  /**
   * Server-issued latest event cursor bound.
   */
  latestEventUlid: string;
}

export const ResetRequired = {
  create(value: DeepPartial<ResetRequired> = {}): ResetRequired {
    return { historyFloorEventUlid: "", latestEventUlid: "", ...value } as ResetRequired;
  },
};

export interface OperatorUpdate {
  /**
   * Server-issued lifecycle event identity.
   */
  eventUlid: string;
  /**
   * @generated from protobuf oneof: change
   */
  change:
    | {
        oneofKind: "runCreated";
        /**
         * @generated from protobuf field: avalanche.operator.RunCreated run_created = 2
         */
        runCreated: RunCreated;
      }
    | {
        oneofKind: "runStatusChanged";
        /**
         * @generated from protobuf field: avalanche.operator.RunStatusChanged run_status_changed = 3
         */
        runStatusChanged: RunStatusChanged;
      }
    | {
        oneofKind: "nodeStatusChanged";
        /**
         * @generated from protobuf field: avalanche.operator.NodeStatusChanged node_status_changed = 4
         */
        nodeStatusChanged: NodeStatusChanged;
      }
    | {
        oneofKind: "logAppended";
        /**
         * @generated from protobuf field: avalanche.operator.LogAppended log_appended = 5
         */
        logAppended: LogAppended;
      }
    | {
        oneofKind: "agentEventAppended";
        /**
         * @generated from protobuf field: avalanche.operator.AgentEventAppended agent_event_appended = 6
         */
        agentEventAppended: AgentEventAppended;
      }
    | {
        oneofKind: "traceFinalized";
        /**
         * @generated from protobuf field: avalanche.operator.TraceFinalized trace_finalized = 7
         */
        traceFinalized: TraceFinalized;
      }
    | {
        oneofKind: "terminalSealAppended";
        terminalSealAppended: TerminalSealAppended;
      }
    | {
        oneofKind: "catalogReplaced";
        /**
         * @generated from protobuf field: avalanche.operator.CatalogReplaced catalog_replaced = 8
         */
        catalogReplaced: CatalogReplaced;
      }
    | {
        oneofKind: "catalogReloadRequired";
        catalogReloadRequired: CatalogReloadRequired;
      }
    | {
        oneofKind: "workflowReloadStatus";
        /**
         * @generated from protobuf field: avalanche.operator.WorkflowReloadStatus workflow_reload_status = 9
         */
        workflowReloadStatus: WorkflowReloadStatus;
      }
    | {
        oneofKind: undefined;
      };
}

export const OperatorUpdate = {
  create(value: DeepPartial<OperatorUpdate> = {}): OperatorUpdate {
    return { eventUlid: "", change: { oneofKind: undefined }, ...value } as OperatorUpdate;
  },
};

export interface OperatorUpdateEnvelope {
  /**
   * @generated from protobuf field: string operator_instance_id = 1
   */
  operatorInstanceId: string;
  /**
   * @generated from protobuf oneof: payload
   */
  payload:
    | {
        oneofKind: "update";
        /**
         * @generated from protobuf field: avalanche.operator.OperatorUpdate update = 2
         */
        update: OperatorUpdate;
      }
    | {
        oneofKind: "resetRequired";
        /**
         * @generated from protobuf field: avalanche.operator.ResetRequired reset_required = 3
         */
        resetRequired: ResetRequired;
      }
    | {
        oneofKind: undefined;
      };
}

export const OperatorUpdateEnvelope = {
  create(value: DeepPartial<OperatorUpdateEnvelope> = {}): OperatorUpdateEnvelope {
    return {
      operatorInstanceId: "",
      payload: { oneofKind: undefined },
      ...value,
    } as OperatorUpdateEnvelope;
  },
};

export interface ListLogsRequest {
  /**
   * Required snapshot-issued token. Cursors and filters are relative to this snapshot.
   *
   * @generated from protobuf field: string page_token = 1
   */
  pageToken: string;
  /**
   * Exclusive lower log bound for forward and incremental hydration.
   *
   * @generated from protobuf field: uint64 after_sequence = 2
   */
  afterSequence: string;
  /**
   * @generated from protobuf field: uint32 page_size = 3
   */
  pageSize: number;
  /**
   * Exclusive upper log bound for newest-first hydration; zero starts at the snapshot end.
   *
   * @generated from protobuf field: uint64 before_sequence = 4
   */
  beforeSequence: string;
  /**
   * Optional exact node filter. Continuation tokens bind this filter.
   *
   * @generated from protobuf field: string node_id = 5
   */
  nodeId: string;
  /**
   * @generated from protobuf field: avalanche.operator.DescriptorPageOrder order = 6
   */
  order: DescriptorPageOrder;
}

export const ListLogsRequest = {
  create(value: DeepPartial<ListLogsRequest> = {}): ListLogsRequest {
    return {
      pageToken: "",
      afterSequence: "0",
      pageSize: 0,
      beforeSequence: "0",
      nodeId: "",
      order: DescriptorPageOrder.FORWARD,
      ...value,
    } as ListLogsRequest;
  },
};

export interface ListAgentEventsRequest {
  /**
   * Required snapshot-issued token. Cursors are relative to this snapshot.
   *
   * @generated from protobuf field: string page_token = 1
   */
  pageToken: string;
  /**
   * Exclusive lower event bound for forward and incremental hydration.
   *
   * @generated from protobuf field: uint64 after_event_sequence = 2
   */
  afterEventSequence: string;
  /**
   * @generated from protobuf field: uint32 page_size = 3
   */
  pageSize: number;
  /**
   * Exclusive upper event bound for newest-first hydration; zero starts at the snapshot end.
   *
   * @generated from protobuf field: uint64 before_event_sequence = 4
   */
  beforeEventSequence: string;
  /**
   * @generated from protobuf field: avalanche.operator.DescriptorPageOrder order = 5
   */
  order: DescriptorPageOrder;
}

export const ListAgentEventsRequest = {
  create(value: DeepPartial<ListAgentEventsRequest> = {}): ListAgentEventsRequest {
    return {
      pageToken: "",
      afterEventSequence: "0",
      pageSize: 0,
      beforeEventSequence: "0",
      order: DescriptorPageOrder.FORWARD,
      ...value,
    } as ListAgentEventsRequest;
  },
};
