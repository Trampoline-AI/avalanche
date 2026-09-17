import type { OperatorApi, StructuralBaseline } from "../api";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  NodeSnapshotMsg,
  type OperatorUpdate,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
  WorkflowTopologyMsg,
} from "../model";

export function eventUlid(sequence: number | string): string {
  return Number(sequence).toString(16).toUpperCase().padStart(26, "0");
}

export const workflow = FlowInfoMsg.create({
  name: "orders",
  displayName: "Orders",
  workflowId: "flows.py::orders",
  relativeFile: "flows.py",
  nodeIds: ["fetch"],
  graph: { fetch: { children: [] } },
  nodeTypes: { fetch: "step" },
  displayNames: { fetch: "Fetch" },
});
export const summary = RunSummaryMsg.create({
  runId: "run-1",
  workflowId: workflow.workflowId,
  workflowDisplayName: workflow.displayName,
  status: "running",
  createdSequence: "1",
  revision: "1",
});
export const secondSummary = RunSummaryMsg.create({
  ...summary,
  runId: "run-2",
  createdSequence: "2",
});
export const node = NodeSnapshotMsg.create({
  nodeId: "fetch",
  name: "Fetch",
  nodeType: "step",
  status: "running",
  revision: "1",
  eventPageToken: "events",
});
export const topology = WorkflowTopologyMsg.create({
  nodeIds: workflow.nodeIds,
  graph: workflow.graph,
  nodeTypes: workflow.nodeTypes,
  displayNames: workflow.displayNames,
});
export const baseline: StructuralBaseline = {
  catalog: CatalogSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfEventUlid: eventUlid(1),
    revision: "1",
    workflows: [workflow],
  }),
  asOfEventUlid: eventUlid(1),
  runs: [summary],
};

export function snapshotFor(run = summary): RunSnapshotMsg {
  return RunSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfEventUlid: baseline.asOfEventUlid,
    summary: run,
    nodes: [node],
    topology,
  });
}

export function envelope(
  sequence: number | string,
  change: OperatorUpdate["change"] = { oneofKind: undefined },
  operatorInstanceId = "operator-1",
): OperatorUpdateEnvelope {
  return OperatorUpdateEnvelope.create({
    operatorInstanceId,
    payload: { oneofKind: "update", update: { eventUlid: eventUlid(sequence), change } },
  });
}

export async function* idleUpdates(
  signal?: AbortSignal,
): AsyncIterable<OperatorUpdateEnvelope> {
  yield* await new Promise<OperatorUpdateEnvelope[]>((resolve) => {
    if (signal?.aborted) resolve([]);
    else signal?.addEventListener("abort", () => resolve([]), { once: true });
  });
}

export function createApi(overrides: Partial<OperatorApi> = {}): OperatorApi {
  return {
    getCatalog: async () => baseline.catalog,
    loadBaseline: async () => baseline,
    getLatestRunSnapshot: async (runId) =>
      snapshotFor(runId === secondSummary.runId ? secondSummary : summary),
    streamUpdates: (_instance, _cursor, signal) => idleUpdates(signal),
    getWorkflowNodeSource: async () => undefined,
    listLogPage: async () => ({
      operatorInstanceId: "operator-1",
      asOfEventUlid: baseline.asOfEventUlid,
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    listAgentEventPage: async () => ({
      operatorInstanceId: "operator-1",
      asOfEventUlid: baseline.asOfEventUlid,
      runId: summary.runId,
      nodeId: node.nodeId,
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    listClassifierEventPage: async () => ({
      operatorInstanceId: "operator-1",
      asOfEventUlid: baseline.asOfEventUlid,
      runId: summary.runId,
      nodeId: node.nodeId,
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    readJsonDetail: async () => undefined,
    readTextDetail: async () => "",
    startRun: async () => "run-3",
    cancelRun: async () => undefined,
    ...overrides,
  };
}
