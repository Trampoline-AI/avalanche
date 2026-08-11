import { GrpcWebFetchTransport } from "@protobuf-ts/grpcweb-transport";

import { DescriptorPageOrder } from "./generated/operator";

import { OperatorServiceClient } from "./generated/operator.client";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  ListAgentEventsRequest,
  ListLogsRequest,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./generated/operator";

const MAX_BASELINE_PAGES = 100;
const MAX_BASELINE_SUMMARIES = 10_000;
const MAX_BASELINE_BYTES = 8 * 1024 * 1024;

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

export class GrpcWebOperatorApi implements OperatorApi {
  readonly client: OperatorServiceClient;

  constructor(baseUrl = window.location.origin, client?: OperatorServiceClient) {
    this.client =
      client ??
      new OperatorServiceClient(new GrpcWebFetchTransport({ baseUrl, format: "binary" }));
  }

  async getCatalog(signal?: AbortSignal): Promise<CatalogSnapshotMsg> {
    return await this.client.getCatalog({}, signal ? { abort: signal } : undefined).response;
  }

  async loadBaseline(signal?: AbortSignal): Promise<StructuralBaseline> {
    const catalog = await this.getCatalog(signal);
    const runs: RunSummaryMsg[] = [];
    const seenPageTokens = new Set<string>();
    let pageToken = "";
    let operatorInstanceId = "";
    let asOfSequence = "0";
    let summaryBytes = 0;
    do {
      if (seenPageTokens.size >= MAX_BASELINE_PAGES) {
        throw new Error("Run baseline exceeds the page hydration budget");
      }
      if (seenPageTokens.has(pageToken)) {
        throw new Error("Run summary pagination made no progress");
      }
      seenPageTokens.add(pageToken);
      const page = await this.client.listRunSummaries(
        {
          workflowSelector: "",
          pageSize: 100,
          pageToken,
        },
        signal ? { abort: signal } : undefined,
      ).response;
      if (!operatorInstanceId) {
        operatorInstanceId = page.operatorInstanceId;
        asOfSequence = page.asOfSequence;
      } else if (
        page.operatorInstanceId !== operatorInstanceId ||
        page.asOfSequence !== asOfSequence
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
        runs.push(summary);
        summaryBytes += encodedBytes;
      }
      pageToken = page.nextPageToken;
    } while (pageToken);

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
    return await this.client.getLatestRunSnapshot(
      { runId, operatorInstanceId },
      signal ? { abort: signal } : undefined,
    ).response;
  }

  streamUpdates(
    operatorInstanceId: string,
    afterSequence: string,
    signal?: AbortSignal,
  ): AsyncIterable<OperatorUpdateEnvelope> {
    return this.client.streamOperatorUpdates(
      { operatorInstanceId, afterSequence },
      signal ? { abort: signal } : undefined,
    ).responses;
  }

  async listLogPage(request: LogPageRequest, signal?: AbortSignal): Promise<LogDescriptorPage> {
    const { expectedOperatorInstanceId, expectedAsOfSequence, ...rpcRequest } = request;
    const page = await this.client.listLogs(rpcRequest, signal ? { abort: signal } : undefined)
      .response;
    if (
      page.operatorInstanceId !== expectedOperatorInstanceId ||
      page.asOfSequence !== expectedAsOfSequence
    ) {
      throw new Error("Log page does not belong to the selected run snapshot");
    }
    const currentCursor =
      request.order === DescriptorPageOrder.NEWEST_FIRST
        ? request.beforeSequence
        : request.afterSequence;
    const nextCursor = page.logs.at(-1)?.sequence ?? currentCursor;
    assertPageProgress(
      request.pageToken,
      page.nextPageToken,
      currentCursor,
      nextCursor,
      request.order,
      page.logs.length,
      "Log",
    );
    return {
      operatorInstanceId: page.operatorInstanceId,
      asOfSequence: page.asOfSequence,
      records: page.logs,
      nextPageToken: page.nextPageToken,
      nextCursor,
    };
  }

  async listAgentEventPage(
    request: AgentEventPageRequest,
    signal?: AbortSignal,
  ): Promise<AgentEventDescriptorPage> {
    const {
      expectedOperatorInstanceId,
      expectedAsOfSequence,
      expectedRunId,
      expectedNodeId,
      ...rpcRequest
    } = request;
    const page = await this.client.listAgentEvents(
      rpcRequest,
      signal ? { abort: signal } : undefined,
    ).response;
    if (
      page.operatorInstanceId !== expectedOperatorInstanceId ||
      page.asOfSequence !== expectedAsOfSequence ||
      page.runId !== expectedRunId ||
      page.nodeId !== expectedNodeId
    ) {
      throw new Error("Agent event page does not belong to the selected node snapshot");
    }
    const currentCursor =
      request.order === DescriptorPageOrder.NEWEST_FIRST
        ? request.beforeEventSequence
        : request.afterEventSequence;
    const nextCursor = page.events.at(-1)?.eventSequence ?? currentCursor;
    assertPageProgress(
      request.pageToken,
      page.nextPageToken,
      currentCursor,
      nextCursor,
      request.order,
      page.events.length,
      "Agent event",
    );
    return {
      operatorInstanceId: page.operatorInstanceId,
      asOfSequence: page.asOfSequence,
      runId: page.runId,
      nodeId: page.nodeId,
      records: page.events,
      nextPageToken: page.nextPageToken,
      nextCursor,
    };
  }

  async readJsonDetail(bodyToken: string, signal?: AbortSignal): Promise<unknown> {
    return JSON.parse(await this.readTextDetail(bodyToken, signal));
  }

  async readTextDetail(bodyToken: string, signal?: AbortSignal): Promise<string> {
    const decoder = new TextDecoder();
    const decoded: string[] = [];
    for await (const chunk of this.client.readDetail(
      { bodyToken },
      signal ? { abort: signal } : undefined,
    ).responses) {
      decoded.push(decoder.decode(chunk.data, { stream: true }));
    }
    decoded.push(decoder.decode());
    return decoded.join("");
  }

  async startRun(workflowSelector: string, input?: Record<string, unknown>): Promise<string> {
    const response = await this.client.startRun({
      workflowSelector,
      runId: "",
      inputJson: input === undefined ? "" : JSON.stringify(input),
      contextJson: "",
      inputFiles: [],
    }).response;
    return response.runId;
  }

  async cancelRun(runId: string): Promise<void> {
    await this.client.cancelRun({ runId }).response;
  }
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
