import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
const { scrollToIndex } = vi.hoisted(() => ({ scrollToIndex: vi.fn() }));


vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 64,
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 120) }, (_, index) => ({
        index,
        start: index * 64,
      })),
    scrollToIndex,
  }),
}));

import type { AgentEventDescriptorPage, OperatorApi } from "./api";
import {
  DescriptorPageOrder,
  type AgentEventDescriptorMsg,
  type CatalogSnapshotMsg,
  type FlowInfoMsg,
  type LogRecordDescriptorMsg,
  type RunSnapshotMsg,
} from "./generated/operator";
import { Inspector } from "./Inspector";

const declaration = JSON.stringify({
  signature: {
    name: "Analyze",
    inputs: [{ name: "question", type: "str", description: "Question to answer" }],
    outputs: [{ name: "answer", type: "str", description: "Final answer" }],
  },
});

const fieldSchemas = JSON.stringify({
  inputs: [{ name: "question", type: "str", description: "Question to answer" }],
  outputs: [{ name: "answer", type: "str", description: "Final answer" }],
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
    agentFieldSchemasJson: { agent_1: fieldSchemas },
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
    getLatestRunSnapshot: async () => run,
    streamUpdates: async function* () {
      return;
    },
    listAgentEventPage: async () => ({
      operatorInstanceId: run.operatorInstanceId,
      asOfSequence: run.asOfSequence,
      runId: run.summary!.runId,
      nodeId: "agent_1",
      records: events,
      nextPageToken: "",
      nextCursor: events.at(-1)?.eventSequence ?? "0",
    }),
    listLogPage: async () => ({
      operatorInstanceId: run.operatorInstanceId,
      asOfSequence: run.asOfSequence,
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    readJsonDetail: async (bodyToken) =>
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
    readTextDetail: async (bodyToken) => bodyToken,
    startRun: async () => "unused",
    cancelRun: async () => undefined,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function turn(sequence: number): AgentEventDescriptorMsg {
  return {
    eventSequence: String(sequence),
    sizeBytes: "64",
    bodyToken: `turn-${sequence}`,
    invocationId: "invocation-1",
    eventKind: "iteration.recorded",
    iteration: sequence,
    durationMs: "10",
    toolCount: 1,
    predictCount: 1,
    error: false,
  };
}

function log(sequence: number, nodeId = "agent_1"): LogRecordDescriptorMsg {
  return {
    sequence: String(sequence),
    timestamp: sequence,
    level: "info",
    nodeId,
    sizeBytes: "8",
    bodyToken: `log-${sequence}`,
  };
}

async function expandObjectValues(container: HTMLElement) {
  const objectName = /^Object \(\d+ (?:property|properties)\)$/;
  const firstDisclosure = await within(container).findByRole("button", {
    name: objectName,
    expanded: false,
  });
  fireEvent.click(firstDisclosure);

  let nestedDisclosure = within(container).queryAllByRole("button", {
    name: objectName,
    expanded: false,
  })[0];
  while (nestedDisclosure) {
    fireEvent.click(nestedDisclosure);
    nestedDisclosure = within(container).queryAllByRole("button", {
      name: objectName,
      expanded: false,
    })[0];
  }
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
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();

    expect(screen.getByText("main-model")).toBeInTheDocument();
    const traceHeaderSection = screen
      .getByRole("heading", { name: "Trace header" })
      .closest("section")!;
    const usage = within(traceHeaderSection)
      .getByRole("heading", { name: "Usage" })
      .parentElement!;
    await expandObjectValues(usage);
    expect(screen.getByText("12")).toBeInTheDocument();
    const telemetry = within(traceHeaderSection)
      .getByRole("heading", { name: "Telemetry" })
      .parentElement!;
    await expandObjectValues(telemetry);
    expect(screen.getByText("trace-1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));

    expect(screen.getByText("Declared fields")).toBeInTheDocument();
    expect(screen.getByText("question")).toBeInTheDocument();
    const inputsSection = screen
      .getByRole("heading", { name: "Invocation inputs" })
      .closest("section")!;
    await expandObjectValues(inputsSection);
    await waitFor(() => expect(screen.getByText("Why?")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(screen.getByText("answer")).toBeInTheDocument();
    const outputSection = screen
      .getByRole("heading", { name: "Terminal output" })
      .closest("section")!;
    await expandObjectValues(outputSection);
    await waitFor(() =>
      expect(screen.getByText("/workspace/result.txt")).toBeInTheDocument(),
    );
  });

  it("uses grammatical empty-state copy for singular output", async () => {
    const operatorApi = {
      ...api(),
      listAgentEventPage: async () => ({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: [],
        nextPageToken: "",
        nextCursor: "0",
      }),
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

    fireEvent.click(screen.getByRole("button", { name: "output" }));

    expect(
      await screen.findByText("No retained output is available."),
    ).toBeInTheDocument();
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
    const readJsonDetail = vi.fn(async (token: string) => ({
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
      listAgentEventPage: async () => ({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: events,
        nextPageToken: "",
        nextCursor: events.at(-1)!.eventSequence,
      }),
      readJsonDetail,
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
    const currentTraceSection = () =>
      screen.getByRole("heading", { name: "RunTrace" }).closest("section")!;
    await expandObjectValues(currentTraceSection());
    await waitFor(() => expect(screen.getByText("reasoning-turn-10")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Following live" }));

    async function openTurn(turn: number) {
      const turnLabel = await screen.findByText(new RegExp(`^Turn ${turn}$`));
      const turnButton = turnLabel.closest("button");
      if (!turnButton) throw new Error(`Turn ${turn} button is missing`);
      fireEvent.click(turnButton);
      await expandObjectValues(currentTraceSection());
      await waitFor(() =>
        expect(screen.getByText(`reasoning-turn-${turn}`)).toBeInTheDocument(),
      );
    }

    for (let turn = 1; turn <= 7; turn += 1) await openTurn(turn);
    await openTurn(10);
    for (let turn = 8; turn <= 9; turn += 1) await openTurn(turn);

    await openTurn(10);
    await openTurn(1);

    expect(readJsonDetail.mock.calls.filter(([token]) => token === "turn-10")).toHaveLength(1);
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "turn-1")).toHaveLength(2);
  });

  it("evicts detail bodies by descriptor bytes and bypasses an individually oversized body", async () => {
    const twoMiB = String(2 * 1024 * 1024);
    const events = Array.from({ length: 6 }, (_, index) => ({
      ...turn(index + 1),
      sizeBytes: index === 5 ? String(9 * 1024 * 1024) : twoMiB,
    }));
    const readJsonDetail = vi.fn(async (token: string) => ({ selected: token }));
    render(
      <Inspector
        api={{
          ...api(),
          listAgentEventPage: async () => ({
            operatorInstanceId: run.operatorInstanceId,
            asOfSequence: run.asOfSequence,
            runId: run.summary!.runId,
            nodeId: "agent_1",
            records: events,
            nextPageToken: "",
            nextCursor: "6",
          }),
          readJsonDetail,
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    await waitFor(() =>
      expect(readJsonDetail.mock.calls.filter(([token]) => token === "turn-6")).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "Following live" }));

    async function selectTurn(number: number, expectedReads = 1) {
      fireEvent.click(screen.getByRole("button", { name: new RegExp(`Turn ${number}`) }));
      await waitFor(() =>
        expect(
          readJsonDetail.mock.calls.filter(([token]) => token === `turn-${number}`),
        ).toHaveLength(expectedReads),
      );
    }

    for (let number = 1; number <= 5; number += 1) await selectTurn(number);
    await selectTurn(1, 2);
    await selectTurn(6, 2);

    expect(readJsonDetail.mock.calls.filter(([token]) => token === "turn-1")).toHaveLength(2);
    expect(readJsonDetail.mock.calls.filter(([token]) => token === "turn-6")).toHaveLength(2);
  });

  it("does not hydrate Overview and requests exactly one page for each active tab", async () => {
    const baseApi = api();
    const listAgentEventPage = vi.fn(baseApi.listAgentEventPage);
    const listLogPage = vi.fn(baseApi.listLogPage);
    const readJsonDetail = vi.fn(baseApi.readJsonDetail);
    const readTextDetail = vi.fn(baseApi.readTextDetail);
    const operatorApi: OperatorApi = {
      ...baseApi,
      listAgentEventPage,
      listLogPage,
      readJsonDetail,
      readTextDetail,
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

    expect(listAgentEventPage).not.toHaveBeenCalled();
    expect(listLogPage).not.toHaveBeenCalled();
    expect(readJsonDetail).not.toHaveBeenCalled();
    expect(readTextDetail).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(1));
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      {
        pageToken: "events",
        afterEventSequence: "0",
        beforeEventSequence: "0",
        pageSize: 100,
        order: DescriptorPageOrder.FORWARD,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "9",
        expectedRunId: "run-1",
        expectedNodeId: "agent_1",
      },
      expect.any(AbortSignal),
    );

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(2));
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      {
        pageToken: "events",
        afterEventSequence: "0",
        beforeEventSequence: "0",
        pageSize: 100,
        order: DescriptorPageOrder.NEWEST_FIRST,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "9",
        expectedRunId: "run-1",
        expectedNodeId: "agent_1",
      },
      expect.any(AbortSignal),
    );
    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("button", { name: "logs" }));
    await waitFor(() => expect(listLogPage).toHaveBeenCalledTimes(1));
    expect(listAgentEventPage).toHaveBeenCalledTimes(3);
    expect(listLogPage).toHaveBeenCalledWith(
      {
        pageToken: "logs",
        afterSequence: "0",
        beforeSequence: "0",
        pageSize: 100,
        nodeId: "agent_1",
        order: DescriptorPageOrder.NEWEST_FIRST,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "9",
      },
      expect.any(AbortSignal),
    );
  });

  it("filters logs by exact node and decodes their bodies only as text", async () => {
    const retainedLogs: LogRecordDescriptorMsg[] = [
      {
        sequence: "3",
        timestamp: 3,
        level: "info",
        nodeId: "agent_2",
        sizeBytes: "8",
        bodyToken: "other-node",
      },
      {
        sequence: "2",
        timestamp: 2,
        level: "info",
        nodeId: "",
        sizeBytes: "8",
        bodyToken: "unscoped",
      },
      {
        sequence: "1",
        timestamp: 1,
        level: "info",
        nodeId: "agent_1",
        sizeBytes: "24",
        bodyToken: "plain-log",
      },
    ];
    const readJsonDetail = vi.fn(api().readJsonDetail);
    const readTextDetail = vi.fn(async () => "plain log: not JSON }");
    const operatorApi: OperatorApi = {
      ...api(),
      listLogPage: async () => ({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        records: retainedLogs,
        nextPageToken: "older-logs",
        nextCursor: "1",
      }),
      readJsonDetail,
      readTextDetail,
    };
    render(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveLogs={[retainedLogs[0], retainedLogs[1]]}
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "logs" }));
    const retainedLog = await screen.findByText("#1");
    expect(screen.queryByText("#2")).not.toBeInTheDocument();
    expect(screen.queryByText("#3")).not.toBeInTheDocument();
    fireEvent.click(retainedLog.closest("button")!);

    expect(await screen.findByText("plain log: not JSON }")).toBeInTheDocument();
    expect(readTextDetail).toHaveBeenCalledWith("plain-log", expect.any(AbortSignal));
    expect(readJsonDetail).not.toHaveBeenCalled();
  });

  it("loads terminal output from the first newest page and continues below its exclusive cursor", async () => {
    const terminal: AgentEventDescriptorMsg = {
      eventSequence: "151",
      sizeBytes: "64",
      bodyToken: "terminal-output",
      invocationId: "invocation-1",
      eventKind: "run.succeeded",
      toolCount: 0,
      predictCount: 0,
      error: false,
    };
    const priorEvents = Array.from({ length: 150 }, (_, index) => turn(index + 1));
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => ({
      operatorInstanceId: run.operatorInstanceId,
      asOfSequence: run.asOfSequence,
      runId: run.summary!.runId,
      nodeId: "agent_1",
      records:
        request.pageToken === "events"
          ? [terminal, ...priorEvents.slice(51).reverse()]
          : priorEvents.slice(0, 51).reverse(),
      nextPageToken: request.pageToken === "events" ? "events-older" : "",
      nextCursor: request.pageToken === "events" ? "52" : "1",
    }));
    const readJsonDetail = vi.fn(async () => ({
      outputs: { answer: "terminal-on-first-page" },
    }));
    render(
      <Inspector
        api={{ ...api(), listAgentEventPage, readJsonDetail }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    const outputSection = screen
      .getByRole("heading", { name: "Terminal output" })
      .closest("section")!;
    await expandObjectValues(outputSection);
    expect(await screen.findByText("terminal-on-first-page")).toBeInTheDocument();
    expect(listAgentEventPage).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        afterEventSequence: "0",
        beforeEventSequence: "0",
        order: DescriptorPageOrder.NEWEST_FIRST,
      }),
      expect.any(AbortSignal),
    );

    fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(2));
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        pageToken: "events-older",
        afterEventSequence: "0",
        beforeEventSequence: "52",
        order: DescriptorPageOrder.NEWEST_FIRST,
      }),
      expect.any(AbortSignal),
    );
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
  });

  it("loads one forward event page per action and appends it in deduplicated order", async () => {
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      const continuation = request.pageToken === "events-next";
      return {
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: continuation ? [turn(4), turn(2)] : [turn(3), turn(1), turn(2)],
        nextPageToken: continuation ? "" : "events-next",
        nextCursor: continuation ? "4" : "3",
      };
    });
    render(
      <Inspector
        api={{ ...api(), listAgentEventPage }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveEvents={[turn(3)]}
        onClose={() => undefined}
      />,
    );

    expect(listAgentEventPage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    const loadMore = await screen.findByRole("button", { name: "Load more events" });
    expect(listAgentEventPage).toHaveBeenCalledTimes(1);
    expect(
      screen
        .getAllByRole("button")
        .filter((button) => button.classList.contains("turn-row"))
        .map((button) => button.querySelector("strong")?.textContent),
    ).toEqual(["Turn 1", "Turn 2", "Turn 3"]);

    fireEvent.click(loadMore);
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(2));
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      {
        pageToken: "events-next",
        afterEventSequence: "3",
        beforeEventSequence: "0",
        pageSize: 100,
        order: DescriptorPageOrder.FORWARD,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "9",
        expectedRunId: "run-1",
        expectedNodeId: "agent_1",
      },
      expect.any(AbortSignal),
    );
    await waitFor(() =>
      expect(
        screen
          .getAllByRole("button")
          .filter((button) => button.classList.contains("turn-row"))
          .map((button) => button.querySelector("strong")?.textContent),
      ).toEqual(["Turn 1", "Turn 2", "Turn 3", "Turn 4"]),
    );
    expect(screen.queryByRole("button", { name: "Load more events" })).not.toBeInTheDocument();
    expect(listAgentEventPage).toHaveBeenCalledTimes(2);
  });

  it("loads older logs once with the cursor, filters exactly, and bounds mounted rows", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => log(121 - index));
    const secondPage = [log(22), ...Array.from({ length: 21 }, (_, index) => log(21 - index))];
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async (request) => {
      const continuation = request.pageToken === "logs-next";
      return {
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        records: continuation
          ? [log(1_000, "agent_10"), ...secondPage]
          : [log(999, "agent_2"), ...firstPage],
        nextPageToken: continuation ? "" : "logs-next",
        nextCursor: continuation ? "1" : "22",
      };
    });
    const view = render(
      <Inspector
        api={{ ...api(), listLogPage }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveLogs={[log(121), log(998, "agent_10")]}
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "logs" }));
    const loadOlder = await screen.findByRole("button", { name: "Load older logs" });
    expect(listLogPage).toHaveBeenCalledTimes(1);
    expect(view.container.querySelectorAll(".log-row")).toHaveLength(100);
    expect(screen.queryByText("#999")).not.toBeInTheDocument();
    expect(screen.queryByText("#998")).not.toBeInTheDocument();

    fireEvent.click(loadOlder);
    await waitFor(() => expect(listLogPage).toHaveBeenCalledTimes(2));
    expect(listLogPage).toHaveBeenLastCalledWith(
      {
        pageToken: "logs-next",
        afterSequence: "0",
        beforeSequence: "22",
        pageSize: 100,
        nodeId: "agent_1",
        order: DescriptorPageOrder.NEWEST_FIRST,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "9",
      },
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(view.container.querySelectorAll(".log-row")).toHaveLength(120));
    const mountedSequences = [...view.container.querySelectorAll(".log-row")].map((row) =>
      Number(row.textContent?.match(/#(\d+)/)?.[1]),
    );
    expect(mountedSequences).toEqual(
      [...mountedSequences].sort((left, right) => right - left),
    );
    expect(new Set(mountedSequences).size).toBe(mountedSequences.length);
    expect(mountedSequences[0]).toBe(121);
    expect(mountedSequences.at(-1)).toBe(2);
    expect(screen.queryByText("#1000")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older logs" })).not.toBeInTheDocument();
    expect(listLogPage).toHaveBeenCalledTimes(2);
  });

  it("caps event and log descriptor windows while retaining the selected edge record", async () => {
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      const start = Number(request.afterEventSequence);
      const records = Array.from({ length: 100 }, (_, index) => turn(start + index + 1));
      const nextCursor = records.at(-1)!.eventSequence;
      return {
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records,
        nextPageToken: nextCursor === "600" ? "" : `events-${nextCursor}`,
        nextCursor,
      };
    });
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async (request) => {
      const upper = request.beforeSequence === "0" ? 600 : Number(request.beforeSequence) - 1;
      const lower = Math.max(1, upper - 99);
      const records = Array.from({ length: upper - lower + 1 }, (_, index) =>
        log(upper - index),
      );
      return {
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        records,
        nextPageToken: lower === 1 ? "" : `logs-${lower}`,
        nextCursor: String(lower),
      };
    });
    const liveEvents = Array.from({ length: 256 }, (_, index) => turn(501 + index));
    const liveLogs = Array.from({ length: 256 }, (_, index) => log(601 + index));

    const view = render(
      <Inspector
        api={{
          ...api(),
          listAgentEventPage,
          listLogPage,
          readJsonDetail: async (token) => ({ selected: token }),
          readTextDetail: async (token) => `selected ${token}`,
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveEvents={liveEvents}
        liveLogs={liveLogs}
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    const firstTurnLabel = await screen.findByText("Turn 1");
    const firstTurn = firstTurnLabel.closest("button");
    if (!firstTurn) throw new Error("Turn 1 button is missing");
    fireEvent.click(screen.getByRole("button", { name: "Following live" }));
    fireEvent.click(firstTurn);
    for (let page = 2; page <= 5; page += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
      await waitFor(() => {
        expect(listAgentEventPage).toHaveBeenCalledTimes(page);
        if (page < 5) {
          expect(screen.getByRole("button", { name: "Load more events" })).not.toBeDisabled();
        }
      });
    }
    await waitFor(() => expect(screen.getByText("500 complete turns")).toBeInTheDocument());
    expect(firstTurn).toHaveClass("active");
    expect(screen.queryByRole("button", { name: /^Turn 257\b/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "logs" }));
    const newestLog = await screen.findByText("#856");
    fireEvent.click(newestLog.closest("button")!);
    expect(await screen.findByText("selected log-856")).toBeInTheDocument();
    for (let page = 2; page <= 5; page += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Load older logs" }));
      await waitFor(() => {
        expect(listLogPage).toHaveBeenCalledTimes(page);
        if (page < 5) {
          expect(screen.getByRole("button", { name: "Load older logs" })).not.toBeDisabled();
        }
      });
    }
    await waitFor(() =>
      expect(view.container.querySelector<HTMLElement>(".log-list > div")?.style.height).toBe(
        `${500 * 64}px`,
      ),
    );
    expect(screen.getByText("#856")).toBeInTheDocument();
    expect(screen.queryByText("#600")).not.toBeInTheDocument();
    expect(screen.getByText("selected log-856")).toBeInTheDocument();
  }, 30_000);

  it("aborts a continuation on snapshot repair and suppresses its stale page", async () => {
    const pendingContinuation = deferred<AgentEventDescriptorPage>();
    let continuationSignal: AbortSignal | undefined;
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>((request, signal) => {
      if (request.pageToken === "events-next") {
        continuationSignal = signal;
        return pendingContinuation.promise;
      }
      const repaired = request.pageToken === "events-repaired";
      return Promise.resolve({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: repaired ? "10" : run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: [turn(repaired ? 7 : 1)],
        nextPageToken: repaired ? "" : "events-next",
        nextCursor: repaired ? "7" : "1",
      });
    });
    const operatorApi = { ...api(), listAgentEventPage };
    const view = render(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    fireEvent.click(await screen.findByRole("button", { name: "Load more events" }));
    await waitFor(() => expect(continuationSignal).toBeDefined());

    const repairedRun: RunSnapshotMsg = {
      ...run,
      asOfSequence: "10",
      nodes: run.nodes.map((item) => ({
        ...item,
        eventPageToken: item.nodeId === "agent_1" ? "events-repaired" : item.eventPageToken,
      })),
    };
    view.rerender(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={repairedRun}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );
    expect(continuationSignal!.aborted).toBe(true);
    await waitFor(() => expect(screen.getByRole("button", { name: /Turn 7/ })).toBeInTheDocument());

    await act(async () => {
      pendingContinuation.resolve({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: [turn(99)],
        nextPageToken: "",
        nextCursor: "99",
      });
      await pendingContinuation.promise;
    });
    expect(screen.queryByRole("button", { name: /Turn 99/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Turn 7/ })).toBeInTheDocument();
  });

  it("aborts an inactive page and suppresses its stale completion", async () => {
    const pendingPage = deferred<AgentEventDescriptorPage>();
    let pageSignal: AbortSignal | undefined;
    const operatorApi: OperatorApi = {
      ...api(),
      listAgentEventPage: (_request, signal) => {
        pageSignal = signal;
        return pendingPage.promise;
      },
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
    await waitFor(() => expect(pageSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "logs" }));
    expect(pageSignal!.aborted).toBe(true);

    await act(async () => {
      pendingPage.resolve({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: [
          {
            eventSequence: "99",
            sizeBytes: "8",
            bodyToken: "stale-turn",
            invocationId: "invocation-1",
            eventKind: "iteration.recorded",
            iteration: 99,
            toolCount: 0,
            predictCount: 0,
            error: false,
          },
        ],
        nextPageToken: "",
        nextCursor: "99",
      });
      await pendingPage.promise;
    });
    expect(screen.queryByText("Turn 99")).not.toBeInTheDocument();
  });

  it("aborts superseded JSON detail and ignores its late value", async () => {
    const pendingInput = deferred<unknown>();
    let inputSignal: AbortSignal | undefined;
    const readJsonDetail = vi.fn((token: string, signal?: AbortSignal) => {
      if (token === "input-body") {
        inputSignal = signal;
        return pendingInput.promise;
      }
      return Promise.resolve({ outputs: { answer: "fresh output" } });
    });
    const operatorApi: OperatorApi = {
      ...api(),
      readJsonDetail,
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

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    await waitFor(() => expect(inputSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(inputSignal!.aborted).toBe(true);
    const outputSection = screen
      .getByRole("heading", { name: "Terminal output" })
      .closest("section")!;
    await expandObjectValues(outputSection);
    expect(await screen.findByText("fresh output")).toBeInTheDocument();

    await act(async () => {
      pendingInput.resolve({ inputs: { question: "stale input" } });
      await pendingInput.promise;
    });
    expect(screen.queryByText("stale input")).not.toBeInTheDocument();
    expect(screen.getByText("fresh output")).toBeInTheDocument();
  });

  it("keeps manual Trace following isolated while another tab is active", async () => {
    const traceEvents: AgentEventDescriptorMsg[] = [
      {
        eventSequence: "1",
        sizeBytes: "32",
        bodyToken: "input-body",
        invocationId: "invocation-1",
        eventKind: "run.started",
        toolCount: 0,
        predictCount: 0,
        error: false,
      },
      {
        eventSequence: "2",
        sizeBytes: "32",
        bodyToken: "turn-1",
        invocationId: "invocation-1",
        eventKind: "iteration.recorded",
        iteration: 1,
        toolCount: 0,
        predictCount: 0,
        error: false,
      },
      {
        eventSequence: "3",
        sizeBytes: "32",
        bodyToken: "turn-2",
        invocationId: "invocation-1",
        eventKind: "iteration.recorded",
        iteration: 2,
        toolCount: 0,
        predictCount: 0,
        error: false,
      },
    ];
    const liveTurn: AgentEventDescriptorMsg = {
      eventSequence: "4",
      sizeBytes: "32",
      bodyToken: "turn-3",
      invocationId: "invocation-1",
      eventKind: "iteration.recorded",
      iteration: 3,
      toolCount: 0,
      predictCount: 0,
      error: false,
    };
    const laterLiveTurn: AgentEventDescriptorMsg = {
      ...liveTurn,
      eventSequence: "5",
      bodyToken: "turn-4",
      iteration: 4,
    };
    const readJsonDetail = vi.fn(async (token: string) =>
      token === "input-body"
        ? { inputs: { question: "active input" } }
        : { selected: token },
    );
    const operatorApi: OperatorApi = {
      ...api(),
      listAgentEventPage: async () => ({
        operatorInstanceId: run.operatorInstanceId,
        asOfSequence: run.asOfSequence,
        runId: run.summary!.runId,
        nodeId: "agent_1",
        records: traceEvents,
        nextPageToken: "",
        nextCursor: "3",
      }),
      readJsonDetail,
    };
    scrollToIndex.mockClear();
    const view = render(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    const traceSection = screen
      .getByRole("heading", { name: "RunTrace" })
      .closest("section")!;
    await expandObjectValues(traceSection);
    expect(await screen.findByText("turn-2")).toBeInTheDocument();
    await waitFor(() => expect(scrollToIndex).toHaveBeenLastCalledWith(1, { align: "end" }));
    view.rerender(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveEvents={[liveTurn]}
        onClose={() => undefined}
      />,
    );
    await waitFor(() => expect(scrollToIndex).toHaveBeenLastCalledWith(2, { align: "end" }));
    scrollToIndex.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /Turn 1/ }));
    await expandObjectValues(traceSection);
    expect(await screen.findByText("turn-1")).toBeInTheDocument();
    expect(scrollToIndex).not.toHaveBeenCalled();
    scrollToIndex.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    const inputsSection = screen
      .getByRole("heading", { name: "Invocation inputs" })
      .closest("section")!;
    await expandObjectValues(inputsSection);
    expect(await screen.findByText("active input")).toBeInTheDocument();
    view.rerender(
      <Inspector
        api={operatorApi}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveEvents={[liveTurn, laterLiveTurn]}
        onClose={() => undefined}
      />,
    );
    expect(scrollToIndex).not.toHaveBeenCalled();
    expect(readJsonDetail.mock.calls.some(([token]) => token === "turn-4")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Turn 1/ })).toHaveClass("active"),
    );
    expect(screen.getByRole("button", { name: "Follow latest" })).toBeInTheDocument();
    expect(readJsonDetail.mock.calls.some(([token]) => token === "turn-4")).toBe(false);
    expect(scrollToIndex).not.toHaveBeenCalled();
  });
});
