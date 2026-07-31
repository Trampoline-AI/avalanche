import { describe, expect, it } from "vitest";

import type { StructuralBaseline } from "./api";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  NodeSnapshotMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
  WorkflowTopologyMsg,
} from "./generated/operator";
import { emptyProjection, projectionReducer } from "./state";

const workflow = FlowInfoMsg.create({
  name: "orders",
  displayName: "Orders",
  workflowId: "flows.py::orders",
  rootAlias: "examples",
  relativeFile: "flows.py",
  nodeIds: ["fetch"],
  graph: { fetch: { children: [] } },
  nodeTypes: { fetch: "step" },
  displayNames: { fetch: "Fetch" },
});
const summary = RunSummaryMsg.create({
  runId: "run-1",
  flowName: "orders",
  workflowId: workflow.workflowId,
  workflowDisplayName: workflow.displayName,
  status: "running",
  createdSequence: "1",
  revision: "1",
});
const node = NodeSnapshotMsg.create({
  nodeId: "fetch",
  name: "Fetch",
  nodeType: "step",
  status: "running",
  revision: "1",
});
const topology = WorkflowTopologyMsg.create({
  nodeIds: ["fetch"],
  graph: { fetch: { children: [] } },
  nodeTypes: { fetch: "step" },
  displayNames: { fetch: "Fetch" },
});
const baseline: StructuralBaseline = {
  catalog: CatalogSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfSequence: "1",
    revision: "1",
    workflows: [workflow],
  }),
  asOfSequence: "1",
  runs: [
    RunSnapshotMsg.create({
      operatorInstanceId: "operator-1",
      asOfSequence: "1",
      summary,
      nodes: [node],
      topology,
    }),
  ],
};

describe("projectionReducer", () => {
  it("installs an authoritative replaceable baseline", () => {
    const state = projectionReducer(emptyProjection, { type: "baseline", baseline });

    expect(state.catalog?.revision).toBe("1");
    expect(state.runs[summary.runId].topology).toEqual(topology);
    expect(state.operatorInstanceId).toBe("operator-1");
    expect(state.sequence).toBe("1");
  });

  it("applies ordered run and node changes without replacing recorded topology", () => {
    let state = projectionReducer(emptyProjection, { type: "baseline", baseline });
    state = projectionReducer(state, {
      type: "envelope",
      envelope: OperatorUpdateEnvelope.create({
        operatorInstanceId: "operator-1",
        payload: {
          oneofKind: "update",
          update: {
            sequence: "2",
            change: {
              oneofKind: "nodeStatusChanged",
              nodeStatusChanged: {
                runId: summary.runId,
                nodeId: "fetch",
                status: "failed",
                startedAt: 10,
                endedAt: 12,
                revision: "2",
                error: "source unavailable",
              },
            },
          },
        },
      }),
    });

    expect(state.runs[summary.runId].nodes[0]).toMatchObject({
      status: "failed",
      error: "source unavailable",
    });
    expect(state.runs[summary.runId].topology).toEqual(topology);
  });

  it("replaces the current catalog without mutating historical runs", () => {
    const initial = projectionReducer(emptyProjection, { type: "baseline", baseline });
    const changedWorkflow = FlowInfoMsg.create({
      ...workflow,
      nodeIds: ["fetch", "store"],
      displayNames: { fetch: "Fetch", store: "Store" },
    });
    const state = projectionReducer(initial, {
      type: "envelope",
      envelope: OperatorUpdateEnvelope.create({
        operatorInstanceId: "operator-1",
        payload: {
          oneofKind: "update",
          update: {
            sequence: "2",
            change: {
              oneofKind: "catalogReplaced",
              catalogReplaced: {
                catalog: CatalogSnapshotMsg.create({
                  operatorInstanceId: "operator-1",
                  asOfSequence: "2",
                  revision: "2",
                  workflows: [changedWorkflow],
                }),
              },
            },
          },
        },
      }),
    });

    expect(state.catalog?.workflows[0].nodeIds).toEqual(["fetch", "store"]);
    expect(state.runs[summary.runId].topology?.nodeIds).toEqual(["fetch"]);
  });

  it("rejects sequence gaps and reset notices", () => {
    const state = projectionReducer(emptyProjection, { type: "baseline", baseline });
    const gap = OperatorUpdateEnvelope.create({
      operatorInstanceId: "operator-1",
      payload: {
        oneofKind: "update",
        update: { sequence: "3", change: { oneofKind: undefined } },
      },
    });
    const reset = OperatorUpdateEnvelope.create({
      operatorInstanceId: "operator-1",
      payload: {
        oneofKind: "resetRequired",
        resetRequired: { historyFloor: "2", latestSequence: "8" },
      },
    });

    expect(() => projectionReducer(state, { type: "envelope", envelope: gap })).toThrow(
      "update gap",
    );
    expect(() => projectionReducer(state, { type: "envelope", envelope: reset })).toThrow(
      "structural reset",
    );
  });
});
