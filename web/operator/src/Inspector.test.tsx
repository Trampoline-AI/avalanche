import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 64,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        start: index * 64,
      })),
  }),
}));

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
    {
      eventSequence: "2",
      sizeBytes: "64",
      bodyToken: "output-body",
      invocationId: "invocation-1",
      eventKind: "run.succeeded",
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
    readDetail: async (bodyToken) =>
      bodyToken === "input-body"
        ? { inputs: { question: "Why?" } }
        : {
            outputs: {
              answer: {
                kind: "predict_rlm_file",
                path: "/workspace/result.txt",
              },
            },
          },
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

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(screen.getByText("answer")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("/workspace/result.txt")).toBeInTheDocument(),
    );
  });

  it("hydrates complete turns on demand and evicts the least recently used body", async () => {
    const events: AgentEventDescriptorMsg[] = Array.from({ length: 10 }, (_, index) => ({
      eventSequence: String(index + 1),
      sizeBytes: "64",
      bodyToken: `turn-${index + 1}`,
      invocationId: "invocation-1",
      eventKind: "iteration.recorded",
      iteration: index + 1,
      durationMs: "10",
      toolCount: 1,
      predictCount: 1,
      error: false,
    }));
    const readDetail = vi.fn(async (token: string) => ({
      reasoning: `reasoning-${token}`,
      code: `code-${token}`,
      output: `output-${token}`,
      finish: { reason: "stop" },
      usage: { input_tokens: 4 },
      tool_calls: [{ name: "lookup" }],
      predict_calls: [{ signature: "Answer" }],
    }));
    const operatorApi = {
      ...api(),
      listAgentEvents: async () => events,
      readDetail,
    };
    render(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    await waitFor(() => expect(screen.getByText("reasoning-turn-10")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Following live" }));

    const turnButtons = screen
      .getAllByRole("button")
      .filter((button) => button.classList.contains("turn-row"));
    for (let turn = 1; turn <= 7; turn += 1) {
      fireEvent.click(turnButtons[turn - 1]);
      await waitFor(() =>
        expect(screen.getByText(`reasoning-turn-${turn}`)).toBeInTheDocument(),
      );
    }
    fireEvent.click(turnButtons[9]);
    await waitFor(() => expect(screen.getByText("reasoning-turn-10")).toBeInTheDocument());
    for (let turn = 8; turn <= 9; turn += 1) {
      fireEvent.click(turnButtons[turn - 1]);
      await waitFor(() =>
        expect(screen.getByText(`reasoning-turn-${turn}`)).toBeInTheDocument(),
      );
    }

    fireEvent.click(turnButtons[9]);
    await waitFor(() => expect(screen.getByText("reasoning-turn-10")).toBeInTheDocument());
    fireEvent.click(turnButtons[0]);
    await waitFor(() => expect(screen.getByText("reasoning-turn-1")).toBeInTheDocument());

    expect(readDetail.mock.calls.filter(([token]) => token === "turn-10")).toHaveLength(1);
    expect(readDetail.mock.calls.filter(([token]) => token === "turn-1")).toHaveLength(2);
  });
});
