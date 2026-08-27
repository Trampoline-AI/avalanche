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
  ProjectSummaryCursorV2,
  RunActivityDescriptorV2,
  RunSnapshotV2,
  RunStatusEnvelopeV2,
  RunSummaryPageV2,
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
  TerminalSealMsg,
} from "./model";

const MAX_BASELINE_PAGES = 100;
const MAX_BASELINE_SUMMARIES = 10_000;
const MAX_BASELINE_BYTES = 8 * 1024 * 1024;
const MAX_CATALOG_PAGES = 100;
const EVENT_ULID_PATTERN = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;

export interface StructuralBaseline {
  catalog: CatalogSnapshotMsg;
  asOfEventUlid: string;
  runs: RunSummaryMsg[];
}

export interface LogPageRequest extends ListLogsRequest {
  expectedOperatorInstanceId: string;
  expectedAsOfEventUlid: string;
}

export interface AgentEventPageRequest extends ListAgentEventsRequest {
  expectedOperatorInstanceId: string;
  expectedAsOfEventUlid: string;
  expectedRunId: string;
  expectedNodeId: string;
}

export interface LogDescriptorPage {
  operatorInstanceId: string;
  asOfEventUlid: string;
  records: LogRecordDescriptorMsg[];
  nextPageToken: string;
  nextCursor: string;
}

export interface AgentEventDescriptorPage {
  operatorInstanceId: string;
  asOfEventUlid: string;
  runId: string;
  nodeId: string;
  records: AgentEventDescriptorMsg[];
  nextPageToken: string;
  nextCursor: string;
}

export interface OperatorApi {
  getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg>;
  loadBaseline(signal?: AbortSignal): Promise<StructuralBaseline>;
  getWorkflowNodeSource(
    workflowSelector: string,
    nodeId: string,
    signal?: AbortSignal,
  ): Promise<string | undefined>;
  getLatestRunSnapshot(
    runId: string,
    operatorInstanceId: string,
    signal?: AbortSignal,
  ): Promise<RunSnapshotMsg>;
  streamUpdates(
    operatorInstanceId: string,
    afterEventUlid: string,
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

interface CatalogPageBaseline {
  operatorInstanceId: string;
  cursor: LifecycleCursorV2;
  revision: string;
}

interface ProjectSummaryCursorChain {
  initialized: boolean;
  cursor: ProjectSummaryCursorV2 | undefined;
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
  private readonly eventCursorsByUlid = new Map<string, LifecycleCursorV2>();
  private scopeReference = "";

  constructor(baseUrl = window.location.origin, client?: IOperatorServiceV2Client) {
    this.client =
      client ??
      new OperatorServiceV2Client(new GrpcWebFetchTransport({ baseUrl, format: "binary" }));
  }

  async getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg> {
    const options = signal ? { abort: signal } : undefined;
    const pages: FlowListV2[] = [];
    let continuation: ContinuationRefV2 | undefined;
    let baseline: CatalogPageBaseline | undefined;
    const seenContinuations = new Set<string>();
    do {
      if (pages.length >= MAX_CATALOG_PAGES) {
        throw new Error("Flow catalog exceeds the page hydration budget");
      }
      const request = continuation ? { pageSize: 200, continuation } : { pageSize: 200 };
      const page = await this.client.discoverFlows(request, options).response;
      baseline = this.validateCatalogPage(page, baseline);
      pages.push(page);
      const continuationId = page.nextPage?.continuationId ?? "";
      if (!continuationId) {
        continuation = undefined;
      } else {
        if (seenContinuations.has(continuationId)) {
          throw new Error("Flow catalog pagination made no progress");
        }
        seenContinuations.add(continuationId);
        continuation = page.nextPage;
      }
    } while (continuation);
    return this.catalogFromFlowLists(pages);
  }

  async getWorkflowNodeSource(
    workflowSelector: string,
    nodeId: string,
    signal?: AbortSignal,
  ): Promise<string | undefined> {
    const source = await this.client.getWorkflowNodeSource(
      { workflowSelector, nodeId },
      signal ? { abort: signal } : undefined,
    ).response;
    return source.sourceCode;
  }

  async loadBaseline(signal?: AbortSignal): Promise<StructuralBaseline> {
    const options = signal ? { abort: signal } : undefined;
    const catalog = await this.getCatalog(signal);
    const runs: RunSummaryMsg[] = [];
    const seenContinuations = new Set<string>();
    let continuation: ContinuationRefV2 | undefined;
    let operatorInstanceId = "";
    let asOfEventUlid = "";
    let summaryBytes = 0;
    const summaryCursorChain: ProjectSummaryCursorChain = {
      initialized: false,
      cursor: undefined,
    };
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
      this.validateProjectSummaryCursor(page, summaryCursorChain);
      const pageOperatorInstanceId = this.rememberScope(page.scopeRef);
      const pageAsOfEventUlid = this.rememberCursor(page.cursor);
      if (!operatorInstanceId) {
        operatorInstanceId = pageOperatorInstanceId;
        asOfEventUlid = pageAsOfEventUlid;
      } else if (
        pageOperatorInstanceId !== operatorInstanceId ||
        pageAsOfEventUlid !== asOfEventUlid
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
      catalog.asOfEventUlid > asOfEventUlid
    ) {
      throw new Error("Operator state changed while loading the browser baseline");
    }
    return { catalog, asOfEventUlid, runs };
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
    afterEventUlid: string,
    signal?: AbortSignal,
  ): AsyncIterable<OperatorUpdateEnvelope> {
    const afterCursor = this.cursorForEventUlid(afterEventUlid);
    const request = {
      ...(afterCursor ? { afterCursor } : {}),
      ...(operatorInstanceId ? { scopeRef: { reference: operatorInstanceId } } : {}),
    };
    const call = this.client.watchRunStatus(request, signal ? { abort: signal } : undefined);
    for await (const envelope of call.responses) {
      const mapped = this.mapStatusEnvelope(envelope);
      if (operatorInstanceId && mapped.operatorInstanceId !== operatorInstanceId) {
        throw new Error("Update does not belong to the connected operator scope");
      }
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
    const asOfEventUlid = this.rememberCursor(page.cursor);
    if (
      operatorInstanceId !== request.expectedOperatorInstanceId ||
      asOfEventUlid !== request.expectedAsOfEventUlid
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
      asOfEventUlid,
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
    const asOfEventUlid = this.rememberCursor(page.cursor);
    if (
      operatorInstanceId !== request.expectedOperatorInstanceId ||
      asOfEventUlid !== request.expectedAsOfEventUlid ||
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
      asOfEventUlid,
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
    if (reference && this.scopeReference && reference !== this.scopeReference) {
      throw new Error("Response does not belong to the connected operator scope");
    }
    if (reference) this.scopeReference = reference;
    return reference || this.scopeReference;
  }

  private rememberCursor(cursor: LifecycleCursorV2 | undefined): string {
    if (!cursor) return "";
    if (!this.isCompleteCursor(cursor)) {
      throw new Error("Lifecycle cursor is not a complete baseline binding");
    }
    this.eventCursorsByUlid.set(cursor.eventUlid, cursor);
    return cursor.eventUlid;
  }

  private cursorForEventUlid(eventUlid: string): LifecycleCursorV2 | undefined {
    return this.eventCursorsByUlid.get(eventUlid);
  }

  private validateProjectSummaryCursor(
    page: RunSummaryPageV2,
    chain: ProjectSummaryCursorChain,
  ): void {
    const cursor = page.projectSummaryCursor;
    if (!chain.initialized) {
      chain.cursor = cursor;
      chain.initialized = true;
    } else if (Boolean(cursor) !== Boolean(chain.cursor)) {
      throw new Error(
        "Project summary cursor appeared or disappeared across run summary pages",
      );
    } else if (cursor && chain.cursor && !this.sameProjectSummaryCursor(cursor, chain.cursor)) {
      throw new Error("Project summary cursor changed across run summary pages");
    }

    const nextPage = page.nextPage;
    if (!nextPage?.continuationId) return;
    const continuationCursor = nextPage.projectSummaryCursor;
    if (
      Boolean(continuationCursor) !== Boolean(cursor) ||
      (cursor &&
        continuationCursor &&
        !this.sameProjectSummaryCursor(continuationCursor, cursor))
    ) {
      throw new Error("Project summary continuation cursor does not match its page");
    }
  }

  private sameProjectSummaryCursor(
    left: ProjectSummaryCursorV2,
    right: ProjectSummaryCursorV2,
  ): boolean {
    return (
      left.stream === right.stream &&
      left.topologyFingerprint === right.topologyFingerprint &&
      left.sourceGeneration === right.sourceGeneration &&
      left.retainedFloorSequence === right.retainedFloorSequence &&
      left.targetHeadSequence === right.targetHeadSequence &&
      left.checkpointWatermark === right.checkpointWatermark &&
      left.checkpointDigest === right.checkpointDigest
    );
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
      cursor.stream === "operator-events" &&
      cursor.topologyFingerprint &&
      cursor.streamGeneration !== "0" &&
      EVENT_ULID_PATTERN.test(cursor.retainedFloorEventUlid) &&
      EVENT_ULID_PATTERN.test(cursor.eventUlid) &&
      cursor.eventUlid >= cursor.retainedFloorEventUlid,
    );
  }

  private validateCatalogPage(
    page: FlowListV2,
    baseline: CatalogPageBaseline | undefined,
  ): CatalogPageBaseline {
    const operatorInstanceId = page.scopeRef?.reference ?? "";
    const cursor = page.cursor;
    if (!operatorInstanceId || !cursor || !this.isCompleteCursor(cursor)) {
      throw new Error("Flow catalog page is not a complete baseline binding");
    }
    const resolvedBaseline = baseline ?? {
      operatorInstanceId,
      cursor,
      revision: page.revision,
    };
    if (
      resolvedBaseline.operatorInstanceId !== operatorInstanceId ||
      resolvedBaseline.revision !== page.revision ||
      !this.sameCursor(resolvedBaseline.cursor, cursor)
    ) {
      throw new Error("Flow catalog changed while loading pages");
    }
    const continuation = page.nextPage;
    if (
      continuation?.continuationId &&
      (continuation.scopeRef?.reference !== operatorInstanceId ||
        !continuation.cursor ||
        !this.sameCursor(continuation.cursor, cursor))
    ) {
      throw new Error("Flow catalog continuation does not match its page baseline");
    }
    return resolvedBaseline;
  }

  private sameCursor(left: LifecycleCursorV2, right: LifecycleCursorV2): boolean {
    return (
      left.stream === right.stream &&
      left.topologyFingerprint === right.topologyFingerprint &&
      left.streamGeneration === right.streamGeneration &&
      left.retainedFloorEventUlid === right.retainedFloorEventUlid &&
      left.eventUlid === right.eventUlid
    );
  }

  private sameContinuation(left: ContinuationRefV2, right: ContinuationRefV2): boolean {
    if (!left.cursor || !right.cursor) return false;
    return (
      left.scopeRef?.reference === right.scopeRef?.reference &&
      left.continuationId === right.continuationId &&
      this.sameCursor(left.cursor, right.cursor)
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
    let baseline: CatalogPageBaseline | undefined;
    for (const page of pages) {
      baseline = this.validateCatalogPage(page, baseline);
    }
    if (!baseline) {
      throw new Error("Flow catalog did not return a page");
    }
    for (const page of pages) {
      flows.push(...page.flows);
      scanTargets.push(...page.scanTargets);
      diagnostics.push(...page.diagnostics);
      this.rememberScope(page.scopeRef);
      this.rememberCursor(page.cursor);
    }
    return {
      operatorInstanceId: baseline.operatorInstanceId,
      asOfEventUlid: baseline.cursor.eventUlid,
      revision: baseline.revision !== "0" ? baseline.revision : catalogRevision(flows),
      workflows: flows.map(mapFlowInfo),
      scanTargets,
      diagnostics,
    };
  }

  private mapRunSnapshot(snapshot: RunSnapshotV2): RunSnapshotMsg {
    const runId = snapshot.summary?.runId ?? "";
    const mapped: RunSnapshotMsg = {
      operatorInstanceId: this.rememberScope(snapshot.scopeRef),
      asOfEventUlid: this.rememberCursor(snapshot.cursor),
      nodes: snapshot.nodes.map((node) => this.mapNodeSnapshot(runId, node)),
      latestLogSequence: snapshot.latestLogSequence,
      logPageToken: this.registerContinuation(snapshot.logContinuation, runId, ""),
    };
    if (snapshot.summary) mapped.summary = mapRunSummary(snapshot.summary);
    if (snapshot.topology) mapped.topology = snapshot.topology;
    if (snapshot.terminalSeal) {
      mapped.terminalSeal = this.mapTerminalSealDescriptor(snapshot.terminalSeal);
    }
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
    if (activity.kind !== "log") {
      throw new Error(`Expected log activity, received ${activity.kind || "missing kind"}`);
    }
    if (!activity.detailRef) {
      throw new Error("Log activity is missing its detail reference");
    }
    return {
      sequence: activity.runSequence,
      timestamp: activity.timestamp,
      level: activity.level,
      nodeId: activity.nodeId,
      sizeBytes: activity.sizeBytes,
      bodyToken: this.registerDetailRef(activity.detailRef),
    };
  }

  private mapAgentEventDescriptor(activity: RunActivityDescriptorV2): AgentEventDescriptorMsg {
    if (activity.kind !== "agent_event") {
      throw new Error(
        `Expected agent event activity, received ${activity.kind || "missing kind"}`,
      );
    }
    if (!activity.detailRef) {
      throw new Error("Agent event activity is missing its detail reference");
    }
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

  private mapTerminalSealDescriptor(activity: RunActivityDescriptorV2): TerminalSealMsg {
    if (activity.kind !== "terminal_seal") {
      throw new Error(
        `Expected terminal seal activity, received ${activity.kind || "missing kind"}`,
      );
    }
    const seal = activity.terminalSeal;
    let hasPositiveRunSequence: boolean;
    try {
      hasPositiveRunSequence = BigInt(activity.runSequence) > 0n;
    } catch {
      hasPositiveRunSequence = false;
    }
    if (
      !activity.activityId ||
      !hasPositiveRunSequence ||
      !Number.isFinite(activity.timestamp) ||
      activity.sizeBytes !== "0" ||
      activity.detailRef ||
      activity.nodeId ||
      activity.level ||
      activity.invocationId ||
      activity.iteration !== undefined ||
      activity.durationMs !== undefined ||
      activity.error ||
      activity.toolCount !== 0 ||
      activity.predictCount !== 0 ||
      activity.eventKind ||
      activity.trace ||
      !seal ||
      !["success", "failed", "cancelled"].includes(seal.terminalStatus)
    ) {
      throw new Error("Terminal seal activity is not a complete typed descriptor");
    }
    return {
      activityId: activity.activityId,
      runSequence: activity.runSequence,
      timestamp: activity.timestamp,
      terminalStatus: seal.terminalStatus as TerminalSealMsg["terminalStatus"],
      ...(seal.reason !== undefined ? { reason: seal.reason } : {}),
    };
  }

  private mapStatusEnvelope(envelope: RunStatusEnvelopeV2): OperatorUpdateEnvelope {
    const operatorInstanceId = this.rememberScope(envelope.scopeRef);
    if (!operatorInstanceId) {
      throw new Error("Update omitted its operator scope reference");
    }
    const eventUlid = this.rememberCursor(envelope.cursor);
    if (envelope.eventUlid !== eventUlid) {
      throw new Error("Update event ULID does not match its cursor");
    }
    const payload = envelope.payload;
    const update = (change: OperatorUpdate["change"]): OperatorUpdateEnvelope => ({
      operatorInstanceId,
      payload: { oneofKind: "update", update: { eventUlid, change } },
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
        if (!activity || !runId) {
          throw new Error("Activity update omitted its run or descriptor");
        }
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
          if (!activity.trace) {
            throw new Error("Trace activity is missing its descriptor");
          }
          if (activity.trace.detailRef) this.registerDetailRef(activity.trace.detailRef);
          return update({
            oneofKind: "traceFinalized",
            traceFinalized: {
              runId,
              nodeId: activity.nodeId,
              trace: activity.trace,
            },
          });
        }
        if (activity.kind === "terminal_seal") {
          return update({
            oneofKind: "terminalSealAppended",
            terminalSealAppended: {
              runId,
              terminalSeal: this.mapTerminalSealDescriptor(activity),
            },
          });
        }
        throw new Error(`Unknown activity kind: ${activity.kind || "missing kind"}`);
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
      case "catalogReloadRequired": {
        const deploymentId = payload.catalogReloadRequired.deploymentId;
        if (!deploymentId) {
          throw new Error("Catalog reload notice omitted its deployment ID");
        }
        return update({
          oneofKind: "catalogReloadRequired",
          catalogReloadRequired: { deploymentId },
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
        if (
          !reset.historyFloor ||
          !reset.latestCursor ||
          !this.isCompleteCursor(reset.historyFloor) ||
          !this.isCompleteCursor(reset.latestCursor) ||
          reset.latestCursor.eventUlid !== envelope.eventUlid ||
          reset.latestCursor.eventUlid !== envelope.cursor?.eventUlid ||
          reset.historyFloor.eventUlid > reset.latestCursor.eventUlid
        ) {
          throw new Error("Reset cursors are not a complete event binding");
        }
        if (reset.historyFloor) this.rememberCursor(reset.historyFloor);
        if (reset.latestCursor) this.rememberCursor(reset.latestCursor);
        return {
          operatorInstanceId,
          payload: {
            oneofKind: "resetRequired",
            resetRequired: {
              historyFloorEventUlid: reset.historyFloor?.eventUlid ?? "",
              latestEventUlid: reset.latestCursor?.eventUlid ?? "",
            },
          },
        };
      }
      default:
        throw new Error("Unknown run status payload");
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
    standardStepDocstringLines: flow.topology?.standardStepDocstringLines ?? {},
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
 * Preserve a deterministic fallback for older test doubles that omit the
 * independent catalog revision field.
 */
function catalogRevision(flows: FlowInfoV2[]): string {
  let hash = 0x811c9dc5;
  const text = flows
    .map((flow) => `${flow.workflowSelector}=${flow.manifestDigest}`)
    .join("\n");
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
