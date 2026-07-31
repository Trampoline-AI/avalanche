import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { OperatorApi } from "./api";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  FlowInfoMsg,
  RunSnapshotMsg,
} from "./generated/operator";
import { Inspector } from "./Inspector";

const declaration = JSON.stringify({
  signature: {
    name: "Analyze",
    inputs: [{ name: "question", type: "str", description: "Question to answer" }],
    outputs: [{ name: "answer", type: "str", description: "Final answer" }],
  },
});

const workflow: FlowInfoMsg = {
  name: "agent_flow",
  filePath: "agent_flow.py",
  nodeIds: ["agent_1"],
  graph: { agent_1: { children: [] } },
  nodeTypes: { agent_1: "step" },
  displayNames: { agent_1: "Agent" },
  cron: "",
  nextRunAt: 0,
  lastRunAt: 0,
  workflowId: "agent_flow.py::agent_flow",
  displayName: "agent_flow",
  rootAlias: "examples",
  relativeFile: "agent_flow.py",
  builderSymbol: "agent_flow",
  agentNodeIds: ["agent_1"],
  agentMetadataJson: { agent_1: declaration },
  webhookPath: "",
  webhookUrl: "",
  webhookActive: false,
};

const run: RunSnapshotMsg = {
  operatorInstanceId: "operator-1",
  asOfSequence: "9",
  summary: {
    runId: "run-1",
    flowName: "agent_flow",
    status: "success",
    startedAt: 10,
    endedAt: 11,
    triggeredBy: "manual",
    workflowId: workflow.workflowId,
    workflowDisplayName: workflow.displayName,
    createdSequence: "2",
    revision: "9",
  },
  nodes: [
    {
      nodeId: "agent_1",
      name: "Agent",
      nodeType: "step",
      status: "success",
      startedAt: 10,
      endedAt: 11,
      trace: {
        status: "completed",
        revision: "4",
        available: true,
        complete: true,
        eventCount: "1",
        sizeBytes: "256",
        latestEventSequence: "1",
        header: {
          status: "completed",
          model: "main-model",
          subModel: "sub-model",
          iterations: "1",
          maxIterations: "4",
          durationMs: "125",
          usageJson: '{"main":{"input_tokens":12}}',
          telemetryJson: '{"trace_id":"trace-1"}',
        },
      },
      revision: "4",
      eventPageToken: "events",
    },
  ],
  latestLogSequence: "0",
  logPageToken: "logs",
  topology: {
    nodeIds: ["agent_1"],
    graph: { agent_1: { children: [] } },
    nodeTypes: { agent_1: "step" },
    displayNames: { agent_1: "Agent" },
    agentMetadataJson: { agent_1: declaration },
  },
};

function api(): OperatorApi {
  const events: AgentEventDescriptorMsg[] = [
    {
      eventSequence: "1",
      sizeBytes: "64",
      bodyToken: "input-body",
      invocationId: "invocation-1",
      eventKind: "run.started",
      toolCount: 0,
      predictCount: 0,
      error: false,
    },
  ];
  return {
    getCatalog: async (): Promise<CatalogSnapshotMsg> => {
      throw new Error("unused");
    },
    loadBaseline: async () => {
      throw new Error("unused");
    },
    streamUpdates: async function* () {
      return;
    },
    listAgentEvents: async () => events,
    listLogs: async () => [],
    readDetail: async () => ({ inputs: { question: "Why?" } }),
    startRun: async () => "unused",
    cancelRun: async () => undefined,
  };
}

describe("Inspector", () => {
  it("renders bounded trace metadata and versioned field associations", async () => {
    render(
      <Inspector
        api={api()}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    expect(screen.getByText("main-model")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("trace-1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));

    expect(screen.getByText("Declared fields")).toBeInTheDocument();
    expect(screen.getByText("question")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Why?")).toBeInTheDocument());
  });
});
