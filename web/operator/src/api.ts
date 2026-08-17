import { GrpcWebFetchTransport } from "@protobuf-ts/grpcweb-transport";

import { PageOrderV2 } from "./generated/operator";
import { DescriptorPageOrder } from "./model";

import {
  OperatorServiceV2Client,
  type IOperatorServiceV2Client,
} from "./generated/operator.client";
import type {
  ActivityDetailRefV2,
  ContinuationRefV2,
  FlowInfoV2,
  FlowListV2,
  LifecycleCursorV2,
  NodeSnapshotV2,
  RunActivityDescriptorV2,
  RunSnapshotV2,
  RunStatusEnvelopeV2,
  RunSummaryV2,
  ScopeReferenceV2,
} from "./generated/operator";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  FlowInfoMsg,
  ListAgentEventsRequest,
  ListLogsRequest,
  LogRecordDescriptorMsg,
  NodeSnapshotMsg,
  OperatorUpdate,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./model";

const MAX_BASELINE_PAGES = 100;
const MAX_BASELINE_SUMMARIES = 10_000;
const MAX_BASELINE_BYTES = 8 * 1024 * 1024;
const MAX_CATALOG_PAGES = 100;

export interface StructuralBaseline {
  catalog: CatalogSnapshotMsg;
  asOfSequence: string;
  runs: RunSummaryMsg[];
}

export interface LogPageRequest extends ListLogsRequest {
  expectedOperatorInstanceId: string;
  expectedAsOfSequence: string;
}

export interface AgentEventPageRequest extends ListAgentEventsRequest {
  expectedOperatorInstanceId: string;
  expectedAsOfSequence: string;
  expectedRunId: string;
  expectedNodeId: string;
}

export interface LogDescriptorPage {
  operatorInstanceId: string;
  asOfSequence: string;
  records: LogRecordDescriptorMsg[];
  nextPageToken: string;
  nextCursor: string;
}

export interface AgentEventDescriptorPage {
  operatorInstanceId: string;
  asOfSequence: string;
  runId: string;
  nodeId: string;
  records: AgentEventDescriptorMsg[];
  nextPageToken: string;
  nextCursor: string;
}

export interface OperatorApi {
  getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg>;
  loadBaseline(signal?: AbortSignal): Promise<StructuralBaseline>;
  getLatestRunSnapshot(
    runId: string,
    operatorInstanceId: string,
    signal?: AbortSignal,
  ): Promise<RunSnapshotMsg>;
  streamUpdates(
    operatorInstanceId: string,
    afterSequence: string,
    signal?: AbortSignal,
  ): AsyncIterable<OperatorUpdateEnvelope>;
  listLogPage(request: LogPageRequest, signal?: AbortSignal): Promise<LogDescriptorPage>;
  listAgentEventPage(
    request: AgentEventPageRequest,
    signal?: AbortSignal,
  ): Promise<AgentEventDescriptorPage>;
  readJsonDetail(bodyToken: string, signal?: AbortSignal): Promise<unknown>;
  readTextDetail(bodyToken: string, signal?: AbortSignal): Promise<string>;
  startRun(workflowSelector: string, input?: Record<string, unknown>): Promise<string>;
  cancelRun(runId: string): Promise<void>;
}

interface RegisteredContinuation {
  continuation: ContinuationRefV2;
  runId: string;
  nodeId: string;
}

export class GrpcWebOperatorApi implements OperatorApi {
  readonly client: IOperatorServiceV2Client;

  /**
   * V2 continuations, detail refs, and lifecycle cursors are rich objects,
   * while the OperatorApi interface exposes them as opaque strings. The
   * registries below retain the full server-issued objects so later interface
   * calls can present the complete binding back to the server.
   */
  private readonly continuations = new Map<string, RegisteredContinuation>();
  private readonly detailRefs = new Map<string, ActivityDetailRefV2>();
  private readonly eventCursorsBySequence = new Map<string, LifecycleCursorV2>();
  private scopeReference = "";

  constructor(
    baseUrl = window.location.origin,
    client?: IOperatorServiceV2Client,
  ) {
    this.client =
      client ??
      new OperatorServiceV2Client(new GrpcWebFetchTransport({ baseUrl, format: "binary" }));
  }

  async getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg> {
    const options = signal ? { abort: signal } : undefined;
    const pages: FlowListV2[] = [];
    let continuation: ContinuationRefV2 | undefined;
    do {
      if (pages.length >= MAX_CATALOG_PAGES) {
        throw new Error("Flow catalog exceeds the page hydration budget");
      }
      const request = continuation ? { pageSize: 200, continuation } : { pageSize: 200 };
      const page = await this.client.discoverFlows(request, options).response;
      pages.push(page);
      continuation = page.nextPage?.continuationId ? page.nextPage : undefined;
    } while (continuation);
    return this.catalogFromFlowLists(pages);
  }

  async loadBaseline(signal?: AbortSignal): Promise<StructuralBaseline> {
    const options = signal ? { abort: signal } : undefined;
    const catalog = await this.getCatalog(signal);
    const runs: RunSummaryMsg[] = [];
    const seenContinuations = new Set<string>();
    let continuation: ContinuationRefV2 | undefined;
    let operatorInstanceId = "";
    let asOfSequence = "0";
    let summaryBytes = 0;
    do {
      if (seenContinuations.size >= MAX_BASELINE_PAGES) {
        throw new Error("Run baseline exceeds the page hydration budget");
      }
      const continuationId = continuation?.continuationId ?? "";
      if (seenContinuations.has(continuationId)) {
        throw new Error("Run summary pagination made no progress");
      }
      seenContinuations.add(continuationId);
      const request = continuation
        ? { workflowSelector: "", pageSize: 100, continuation }
        : { workflowSelector: "", pageSize: 100 };
      const page = await this.client.listRunSummaries(request, options).response;
      const pageOperatorInstanceId = this.rememberScope(page.scopeRef);
      const pageAsOfSequence = this.rememberCursor(page.cursor);
      if (!operatorInstanceId) {
        operatorInstanceId = pageOperatorInstanceId;
        asOfSequence = pageAsOfSequence;
      } else if (
        pageOperatorInstanceId !== operatorInstanceId ||
        pageAsOfSequence !== asOfSequence
      ) {
        throw new Error("Run baseline changed while loading pages");
      }
      for (const summary of page.runs) {
        const encodedBytes = new TextEncoder().encode(JSON.stringify(summary)).byteLength;
        if (
          runs.length >= MAX_BASELINE_SUMMARIES ||
          summaryBytes + encodedBytes > MAX_BASELINE_BYTES
        ) {
          throw new Error("Run baseline exceeds the hydration budget");
        }
        runs.push(mapRunSummary(summary));
        summaryBytes += encodedBytes;
      }
      continuation = page.nextPage?.continuationId ? page.nextPage : undefined;
    } while (continuation);

    const confirmedCatalog = await this.getCatalog(signal);
    if (
      catalog.operatorInstanceId !== operatorInstanceId ||
      confirmedCatalog.operatorInstanceId !== operatorInstanceId ||
      catalog.revision !== confirmedCatalog.revision ||
      BigInt(catalog.asOfSequence) > BigInt(asOfSequence)
    ) {
      throw new Error("Operator state changed while loading the browser baseline");
    }
    return { catalog, asOfSequence, runs };
  }

  async getLatestRunSnapshot(
    runId: string,
    operatorInstanceId: string,
    signal?: AbortSignal,
  ): Promise<RunSnapshotMsg> {
    const snapshot = await this.client.getRunSnapshot(
      { runId },
      signal ? { abort: signal } : undefined,
    ).response;
    const mapped = this.mapRunSnapshot(snapshot);
    if (operatorInstanceId && mapped.operatorInstanceId !== operatorInstanceId) {
      throw new Error("Run snapshot does not belong to the connected operator instance");
    }
    return mapped;
  }

  async *streamUpdates(
    operatorInstanceId: string,
    afterSequence: string,
    signal?: AbortSignal,
  ): AsyncIterable<OperatorUpdateEnvelope> {
    const afterCursor = this.cursorForSequence(afterSequence);
    const request = afterCursor ? { afterCursor } : {};
    const call = this.client.watchRunStatus(
      request,
      signal ? { abort: signal } : undefined,
    );
    for await (const envelope of call.responses) {
      const mapped = this.mapStatusEnvelope(envelope);
      if (!mapped) continue;
      if (operatorInstanceId && mapped.operatorInstanceId !== operatorInstanceId) continue;
      yield mapped;
    }
  }

  async listLogPage(request: LogPageRequest, signal?: AbortSignal): Promise<LogDescriptorPage> {
    const entry = this.continuations.get(request.pageToken);
    if (!entry) {
      throw new Error("Log page token is not bound to a known run snapshot");
    }
    const page = await this.client.listRunActivity(
      {
        runId: entry.runId,
        pageSize: request.pageSize,
        continuation: entry.continuation,
        nodeId: "",
        order: mapPageOrder(request.order),
      },
      signal ? { abort: signal } : undefined,
    ).response;
    const operatorInstanceId = this.rememberScope(page.scopeRef);
    const asOfSequence = this.rememberCursor(page.cursor);
    if (
      operatorInstanceId !== request.expectedOperatorInstanceId ||
      asOfSequence !== request.expectedAsOfSequence
    ) {
      throw new Error("Log page does not belong to the selected run snapshot");
    }
    const records = page.activities.map((activity) => this.mapLogDescriptor(activity));
    const currentCursor =
      request.order === DescriptorPageOrder.NEWEST_FIRST
        ? request.beforeSequence
        : request.afterSequence;
    const nextCursor = records.at(-1)?.sequence ?? currentCursor;
    const nextPageToken = this.registerContinuation(page.nextPage, entry.runId, "");
    assertPageProgress(
      request.pageToken,
      nextPageToken,
      currentCursor,
      nextCursor,
      request.order,
      records.length,
      "Log",
    );
    return {
      operatorInstanceId,
      asOfSequence,
      records,
      nextPageToken,
      nextCursor,
    };
  }

  async listAgentEventPage(
    request: AgentEventPageRequest,
    signal?: AbortSignal,
  ): Promise<AgentEventDescriptorPage> {
    const entry = this.continuations.get(request.pageToken);
    if (!entry) {
      throw new Error("Agent event page token is not bound to a known node snapshot");
    }
    const page = await this.client.listRunActivity(
      {
        runId: entry.runId,
        pageSize: request.pageSize,
        continuation: entry.continuation,
        nodeId: entry.nodeId,
        order: mapPageOrder(request.order),
      },
      signal ? { abort: signal } : undefined,
    ).response;
    const operatorInstanceId = this.rememberScope(page.scopeRef);
    const asOfSequence = this.rememberCursor(page.cursor);
    if (
      operatorInstanceId !== request.expectedOperatorInstanceId ||
      asOfSequence !== request.expectedAsOfSequence ||
      page.runId !== request.expectedRunId ||
      entry.nodeId !== request.expectedNodeId
    ) {
      throw new Error("Agent event page does not belong to the selected node snapshot");
    }
    const records = page.activities.map((activity) => this.mapAgentEventDescriptor(activity));
    const currentCursor =
      request.order === DescriptorPageOrder.NEWEST_FIRST
        ? request.beforeEventSequence
        : request.afterEventSequence;
    const nextCursor = records.at(-1)?.eventSequence ?? currentCursor;
    const nextPageToken = this.registerContinuation(page.nextPage, entry.runId, entry.nodeId);
    assertPageProgress(
      request.pageToken,
      nextPageToken,
      currentCursor,
      nextCursor,
      request.order,
      records.length,
      "Agent event",
    );
    return {
      operatorInstanceId,
      asOfSequence,
      runId: page.runId,
      nodeId: entry.nodeId,
      records,
      nextPageToken,
      nextCursor,
    };
  }

  async readJsonDetail(bodyToken: string, signal?: AbortSignal): Promise<unknown> {
    return JSON.parse(await this.readTextDetail(bodyToken, signal));
  }

  async readTextDetail(bodyToken: string, signal?: AbortSignal): Promise<string> {
    const detailRef = this.detailRefs.get(bodyToken);
    if (!detailRef) {
      throw new Error("Activity detail reference is not bound to a server-issued descriptor");
    }
    const decoder = new TextDecoder();
    const decoded: string[] = [];
    for await (const chunk of this.client.readActivityDetail(
      { detailRef },
      signal ? { abort: signal } : undefined,
    ).responses) {
      decoded.push(decoder.decode(chunk.data, { stream: true }));
    }
    decoded.push(decoder.decode());
    return decoded.join("");
  }

  async startRun(workflowSelector: string, input?: Record<string, unknown>): Promise<string> {
    const runId = `run_${crypto.randomUUID().replaceAll("-", "").slice(0, 8)}`;
    const response = await this.client.startRun({
      runId,
      workflowSelector,
      inputJson: input === undefined ? "" : JSON.stringify(input),
      contextJson: "",
      inputFiles: [],
    }).response;
    return response.runId;
  }

  async cancelRun(runId: string): Promise<void> {
    await this.client.cancelRun({ runId }).response;
  }

  private rememberScope(scopeRef: ScopeReferenceV2 | undefined): string {
    const reference = scopeRef?.reference ?? "";
    if (reference) this.scopeReference = reference;
    return reference || this.scopeReference;
  }

  private rememberCursor(cursor: LifecycleCursorV2 | undefined): string {
    if (!cursor) return "0";
    if (!this.isCompleteCursor(cursor)) {
      throw new Error("Lifecycle cursor is incomplete");
    }
    if (cursor.stream === "operator-events") {
      this.eventCursorsBySequence.set(cursor.sourceSequence, cursor);
    }
    return cursor.sourceSequence;
  }

  private cursorForSequence(sequence: string): LifecycleCursorV2 | undefined {
    return this.eventCursorsBySequence.get(sequence);
  }

  private registerContinuation(
    continuation: ContinuationRefV2 | undefined,
    runId: string,
    nodeId: string,
  ): string {
    if (!continuation || !continuation.continuationId) return "";
    if (
      !continuation.scopeRef?.reference ||
      !continuation.cursor ||
      !this.isCompleteCursor(continuation.cursor)
    ) {
      throw new Error("Continuation is not a complete server-issued binding");
    }
    const existing = this.continuations.get(continuation.continuationId);
    if (
      existing &&
      (!this.sameContinuation(existing.continuation, continuation) ||
        existing.runId !== runId ||
        existing.nodeId !== nodeId)
    ) {
      throw new Error("Continuation binding was replaced by a different target");
    }
    this.continuations.set(continuation.continuationId, { continuation, runId, nodeId });
    if (continuation.cursor) this.rememberCursor(continuation.cursor);
    return continuation.continuationId;
  }

  private registerDetailRef(detailRef: ActivityDetailRefV2 | undefined): string {
    if (!detailRef || !detailRef.objectKey) return "";
    if (
      !detailRef.runId ||
      !detailRef.scopeRef?.reference ||
      !detailRef.activityId ||
      !detailRef.objectUri ||
      detailRef.sha256.length !== 64
    ) {
      throw new Error("Activity detail reference is not a complete immutable binding");
    }
    const existing = this.detailRefs.get(detailRef.objectKey);
    if (existing && !this.sameDetailRef(existing, detailRef)) {
      throw new Error("Activity detail reference binding was replaced");
    }
    this.detailRefs.set(detailRef.objectKey, detailRef);
    return detailRef.objectKey;
  }

  private isCompleteCursor(cursor: LifecycleCursorV2): boolean {
    return Boolean(
      cursor.stream &&
        cursor.topologyFingerprint &&
        cursor.streamGeneration !== "0" &&
        cursor.retainedFloor !== "0",
    );
  }

  private sameContinuation(left: ContinuationRefV2, right: ContinuationRefV2): boolean {
    if (!left.cursor || !right.cursor) return false;
    return (
      left.scopeRef?.reference === right.scopeRef?.reference &&
      left.continuationId === right.continuationId &&
      left.cursor.stream === right.cursor.stream &&
      left.cursor.topologyFingerprint === right.cursor.topologyFingerprint &&
      left.cursor.streamGeneration === right.cursor.streamGeneration &&
      left.cursor.retainedFloor === right.cursor.retainedFloor &&
      left.cursor.sourceSequence === right.cursor.sourceSequence
    );
  }

  private sameDetailRef(left: ActivityDetailRefV2, right: ActivityDetailRefV2): boolean {
    return (
      left.runId === right.runId &&
      left.scopeRef?.reference === right.scopeRef?.reference &&
      left.activityId === right.activityId &&
      left.runSequence === right.runSequence &&
      left.objectUri === right.objectUri &&
      left.objectKey === right.objectKey &&
      left.sha256 === right.sha256 &&
      left.sizeBytes === right.sizeBytes
    );
  }

  private catalogFromFlowLists(pages: FlowListV2[]): CatalogSnapshotMsg {
    const flows: FlowInfoV2[] = [];
    const scanTargets: CatalogSnapshotMsg["scanTargets"] = [];
    const diagnostics: CatalogSnapshotMsg["diagnostics"] = [];
    let operatorInstanceId = "";
    let asOfSequence = "0";
    for (const page of pages) {
      flows.push(...page.flows);
      scanTargets.push(...page.scanTargets);
      diagnostics.push(...page.diagnostics);
      const reference = this.rememberScope(page.scopeRef);
      if (!operatorInstanceId) operatorInstanceId = reference;
      const pageSequence = this.rememberCursor(page.cursor);
      if (page.cursor) asOfSequence = pageSequence;
    }
    return {
      operatorInstanceId,
      asOfSequence,
      revision: catalogRevision(flows),
      workflows: flows.map(mapFlowInfo),
      scanTargets,
      diagnostics,
    };
  }

  private mapRunSnapshot(snapshot: RunSnapshotV2): RunSnapshotMsg {
    const runId = snapshot.summary?.runId ?? "";
    const mapped: RunSnapshotMsg = {
      operatorInstanceId: this.rememberScope(snapshot.scopeRef),
      asOfSequence: this.rememberCursor(snapshot.cursor),
      nodes: snapshot.nodes.map((node) => this.mapNodeSnapshot(runId, node)),
      latestLogSequence: snapshot.latestLogSequence,
      logPageToken: this.registerContinuation(snapshot.logContinuation, runId, ""),
    };
    if (snapshot.summary) mapped.summary = mapRunSummary(snapshot.summary);
    if (snapshot.topology) mapped.topology = snapshot.topology;
    return mapped;
  }

  private mapNodeSnapshot(runId: string, node: NodeSnapshotV2): NodeSnapshotMsg {
    if (node.trace?.detailRef) this.registerDetailRef(node.trace.detailRef);
    const mapped: NodeSnapshotMsg = {
      nodeId: node.nodeId,
      name: node.name,
      nodeType: node.nodeType,
      status: node.status,
      startedAt: node.startedAt,
      endedAt: node.endedAt,
      revision: node.revision,
      eventPageToken: this.registerContinuation(node.activityContinuation, runId, node.nodeId),
    };
    if (node.trace) mapped.trace = node.trace;
    if (node.error !== undefined) mapped.error = node.error;
    if (node.runningElapsedSeconds !== undefined) {
      mapped.runningElapsedSeconds = node.runningElapsedSeconds;
    }
    return mapped;
  }

  private mapLogDescriptor(activity: RunActivityDescriptorV2): LogRecordDescriptorMsg {
    return {
      sequence: activity.runSequence,
      timestamp: activity.timestamp,
      level: activity.level,
      nodeId: activity.nodeId,
      sizeBytes: activity.sizeBytes,
      bodyToken: this.registerDetailRef(activity.detailRef),
    };
  }

  private mapAgentEventDescriptor(
    activity: RunActivityDescriptorV2,
  ): AgentEventDescriptorMsg {
    const mapped: AgentEventDescriptorMsg = {
      eventSequence: activity.runSequence,
      sizeBytes: activity.sizeBytes,
      bodyToken: this.registerDetailRef(activity.detailRef),
      invocationId: activity.invocationId,
      eventKind: activity.eventKind,
      error: activity.error,
      toolCount: activity.toolCount,
      predictCount: activity.predictCount,
    };
    if (activity.iteration !== undefined) mapped.iteration = activity.iteration;
    if (activity.durationMs !== undefined) mapped.durationMs = activity.durationMs;
    return mapped;
  }

  private mapStatusEnvelope(envelope: RunStatusEnvelopeV2): OperatorUpdateEnvelope | undefined {
    const operatorInstanceId = this.rememberScope(envelope.scopeRef);
    const sequence = this.rememberCursor(envelope.cursor);
    const payload = envelope.payload;
    const update = (change: OperatorUpdate["change"]): OperatorUpdateEnvelope => ({
      operatorInstanceId,
      payload: { oneofKind: "update", update: { sequence, change } },
    });
    switch (payload.oneofKind) {
      case "runCreated": {
        const created = payload.runCreated;
        const runId = created.summary?.runId ?? "";
        return update({
          oneofKind: "runCreated",
          runCreated: {
            nodes: created.nodes.map((node) => this.mapNodeSnapshot(runId, node)),
            ...(created.summary ? { summary: mapRunSummary(created.summary) } : {}),
            ...(created.topology ? { topology: created.topology } : {}),
          },
        });
      }
      case "runStatusChanged": {
        const summary = payload.runStatusChanged.summary;
        return update({
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: summary?.runId ?? "",
            status: summary?.status ?? "",
            startedAt: summary?.startedAt ?? 0,
            endedAt: summary?.endedAt ?? 0,
            revision: summary?.revision ?? "0",
          },
        });
      }
      case "nodeStatusChanged": {
        const node = payload.nodeStatusChanged.node;
        return update({
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: payload.nodeStatusChanged.runId,
            nodeId: node?.nodeId ?? "",
            status: node?.status ?? "",
            startedAt: node?.startedAt ?? 0,
            endedAt: node?.endedAt ?? 0,
            revision: node?.revision ?? "0",
            ...(node?.error !== undefined ? { error: node.error } : {}),
            ...(node?.runningElapsedSeconds !== undefined
              ? { runningElapsedSeconds: node.runningElapsedSeconds }
              : {}),
          },
        });
      }
      case "activityAppended": {
        const activity = payload.activityAppended.activity;
        const runId = payload.activityAppended.runId;
        if (!activity) return undefined;
        if (activity.kind === "log") {
          return update({
            oneofKind: "logAppended",
            logAppended: { runId, log: this.mapLogDescriptor(activity) },
          });
        }
        if (activity.kind === "agent_event") {
          return update({
            oneofKind: "agentEventAppended",
            agentEventAppended: {
              runId,
              nodeId: activity.nodeId,
              event: this.mapAgentEventDescriptor(activity),
            },
          });
        }
        if (activity.kind === "trace") {
          if (activity.trace?.detailRef) this.registerDetailRef(activity.trace.detailRef);
          return update({
            oneofKind: "traceFinalized",
            traceFinalized: {
              runId,
              nodeId: activity.nodeId,
              ...(activity.trace ? { trace: activity.trace } : {}),
            },
          });
        }
        return undefined;
      }
      case "flowListChanged": {
        const flowList = payload.flowListChanged.flowList;
        return update({
          oneofKind: "catalogReplaced",
          catalogReplaced: {
            ...(flowList ? { catalog: this.catalogFromFlowLists([flowList]) } : {}),
          },
        });
      }
      case "flowReloadStatus": {
        return update({
          oneofKind: "workflowReloadStatus",
          workflowReloadStatus: { reloading: payload.flowReloadStatus.reloading },
        });
      }
      case "resetRequired": {
        const reset = payload.resetRequired;
        if (reset.historyFloor) this.rememberCursor(reset.historyFloor);
        if (reset.latestCursor) this.rememberCursor(reset.latestCursor);
        return {
          operatorInstanceId,
          payload: {
            oneofKind: "resetRequired",
            resetRequired: {
              historyFloor: reset.historyFloor?.sourceSequence ?? "0",
              latestSequence: reset.latestCursor?.sourceSequence ?? "0",
            },
          },
        };
      }
      default:
        return undefined;
    }
  }
}

function mapPageOrder(order: DescriptorPageOrder): PageOrderV2 {
  return order === DescriptorPageOrder.NEWEST_FIRST
    ? PageOrderV2.NEWEST_FIRST
    : PageOrderV2.FORWARD;
}

function mapRunSummary(summary: RunSummaryV2): RunSummaryMsg {
  return {
    runId: summary.runId,
    flowName: summary.workflowSelector,
    status: summary.status,
    startedAt: summary.startedAt,
    endedAt: summary.endedAt,
    triggeredBy: summary.triggeredBy,
    workflowId: summary.workflowSelector,
    workflowDisplayName: summary.workflowDisplayName,
    createdSequence: summary.createdSequence,
    revision: summary.revision,
    triggeredAt: summary.triggeredAt,
  };
}

function mapFlowInfo(flow: FlowInfoV2): FlowInfoMsg {
  return {
    name: flow.workflowSelector,
    filePath: flow.filePath,
    nodeIds: flow.nodeIds,
    graph: flow.topology?.graph ?? {},
    nodeTypes: flow.topology?.nodeTypes ?? {},
    displayNames: flow.topology?.displayNames ?? {},
    cron: flow.cron,
    nextRunAt: flow.nextRunAt,
    lastRunAt: flow.lastRunAt,
    workflowId: flow.workflowId,
    displayName: flow.displayName,
    rootAlias: "",
    relativeFile: flow.filePath,
    builderSymbol: "",
    agentNodeIds: flow.agentNodeIds,
    agentMetadataJson: flow.agentMetadataJson,
    webhookPath: flow.webhookPath,
    webhookUrl: flow.webhookUrl,
    webhookActive: flow.webhookActive,
  };
}

/**
 * V2 has no catalog revision counter; derive a deterministic content revision
 * from the flow selectors and manifest digests so equality holds across
 * unchanged DiscoverFlows responses.
 */
function catalogRevision(flows: FlowInfoV2[]): string {
  let hash = 0x811c9dc5;
  const text = flows.map((flow) => `${flow.workflowSelector}=${flow.manifestDigest}`).join("\n");
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return String(hash >>> 0);
}

function assertPageProgress(
  pageToken: string,
  nextPageToken: string,
  currentCursor: string,
  nextCursor: string,
  order: DescriptorPageOrder,
  recordCount: number,
  kind: string,
): void {
  if (!nextPageToken) return;
  if (
    nextPageToken === pageToken ||
    recordCount === 0 ||
    nextCursor === currentCursor ||
    (order === DescriptorPageOrder.FORWARD && BigInt(nextCursor) <= BigInt(currentCursor)) ||
    (order === DescriptorPageOrder.NEWEST_FIRST &&
      currentCursor !== "0" &&
      BigInt(nextCursor) >= BigInt(currentCursor))
  ) {
    throw new Error(`${kind} pagination made no progress`);
  }
}
