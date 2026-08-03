import { GrpcWebFetchTransport } from "@protobuf-ts/grpcweb-transport";

import { DescriptorPageOrder } from "./generated/operator";

import { OperatorServiceClient } from "./generated/operator.client";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./generated/operator";

export interface StructuralBaseline {
  catalog: CatalogSnapshotMsg;
  asOfSequence: string;
  runs: RunSnapshotMsg[];
}

export interface OperatorApi {
  getCatalog(): Promise<CatalogSnapshotMsg>;
  loadBaseline(): Promise<StructuralBaseline>;
  streamUpdates(operatorInstanceId: string, afterSequence: string): AsyncIterable<OperatorUpdateEnvelope>;
  listAgentEvents(snapshot: RunSnapshotMsg, nodeId: string): Promise<AgentEventDescriptorMsg[]>;
  listLogs(snapshot: RunSnapshotMsg): Promise<LogRecordDescriptorMsg[]>;
  readDetail(bodyToken: string): Promise<unknown>;
  startRun(workflowSelector: string, input?: Record<string, unknown>): Promise<string>;
  cancelRun(runId: string): Promise<void>;
}

export class GrpcWebOperatorApi implements OperatorApi {
  readonly client: OperatorServiceClient;

  constructor(baseUrl = window.location.origin) {
    const transport = new GrpcWebFetchTransport({ baseUrl, format: "binary" });
    this.client = new OperatorServiceClient(transport);
  }

  async getCatalog(): Promise<CatalogSnapshotMsg> {
    return (await this.client.getCatalog({}).response);
  }

  async loadBaseline(): Promise<StructuralBaseline> {
    const catalog = await this.getCatalog();
    const summaries: RunSummaryMsg[] = [];
    let pageToken = "";
    let operatorInstanceId = "";
    let asOfSequence = "0";
    do {
      const page = await this.client.listRunSummaries({
        workflowSelector: "",
        pageSize: 100,
        pageToken,
      }).response;
      if (!operatorInstanceId) {
        operatorInstanceId = page.operatorInstanceId;
        asOfSequence = page.asOfSequence;
      } else if (
        page.operatorInstanceId !== operatorInstanceId ||
        page.asOfSequence !== asOfSequence
      ) {
        throw new Error("Run baseline changed while loading pages");
      }
      summaries.push(...page.runs);
      pageToken = page.nextPageToken;
    } while (pageToken);

    const runs = await Promise.all(
      summaries.map(
        async (summary) =>
          await this.client.getRunSnapshot({
            runId: summary.runId,
            operatorInstanceId,
            asOfSequence,
          }).response,
      ),
    );
    const confirmedCatalog = await this.getCatalog();
    if (
      catalog.operatorInstanceId !== operatorInstanceId ||
      confirmedCatalog.operatorInstanceId !== operatorInstanceId ||
      catalog.revision !== confirmedCatalog.revision ||
      BigInt(catalog.asOfSequence) > BigInt(asOfSequence) ||
      BigInt(confirmedCatalog.asOfSequence) < BigInt(asOfSequence)
    ) {
      throw new Error("Operator state changed while loading the browser baseline");
    }
    return { catalog, asOfSequence, runs };
  }

  streamUpdates(
    operatorInstanceId: string,
    afterSequence: string,
  ): AsyncIterable<OperatorUpdateEnvelope> {
    return this.client.streamOperatorUpdates({ operatorInstanceId, afterSequence }).responses;
  }

  async listAgentEvents(
    snapshot: RunSnapshotMsg,
    nodeId: string,
  ): Promise<AgentEventDescriptorMsg[]> {
    const node = snapshot.nodes.find((item) => item.nodeId === nodeId);
    if (!node?.eventPageToken) return [];
    const events: AgentEventDescriptorMsg[] = [];
    let pageToken = node.eventPageToken;
    let afterEventSequence = "0";
    do {
      const page = await this.client.listAgentEvents({
        pageToken,
        afterEventSequence,
        pageSize: 100,
        beforeEventSequence: "0",
        order: DescriptorPageOrder.FORWARD,
      }).response;
      events.push(...page.events);
      if (page.events.length) {
        afterEventSequence = page.events.at(-1)!.eventSequence;
      }
      pageToken = page.nextPageToken;
    } while (pageToken);
    return events;
  }

  async listLogs(snapshot: RunSnapshotMsg): Promise<LogRecordDescriptorMsg[]> {
    if (!snapshot.logPageToken) return [];
    const logs: LogRecordDescriptorMsg[] = [];
    let pageToken = snapshot.logPageToken;
    let afterSequence = "0";
    do {
      const page = await this.client.listLogs({
        pageToken,
        afterSequence,
        pageSize: 100,
        beforeSequence: "0",
        nodeId: "",
        order: DescriptorPageOrder.FORWARD,
      }).response;
      logs.push(...page.logs);
      if (page.logs.length) afterSequence = page.logs.at(-1)!.sequence;
      pageToken = page.nextPageToken;
    } while (pageToken);
    return logs;
  }

  async readDetail(bodyToken: string): Promise<unknown> {
    const chunks: Uint8Array[] = [];
    for await (const chunk of this.client.readDetail({ bodyToken }).responses) {
      chunks.push(chunk.data);
    }
    const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
    const body = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.length;
    }
    return JSON.parse(new TextDecoder().decode(body));
  }

  async startRun(
    workflowSelector: string,
    input?: Record<string, unknown>,
  ): Promise<string> {
    const response = await this.client.startRun({
      flowName: "",
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
