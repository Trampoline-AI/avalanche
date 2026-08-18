import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentEventDescriptorPage, OperatorApi } from "./api";
import {
  DescriptorPageOrder,
  type AgentEventDescriptorMsg,
  type CatalogSnapshotMsg,
  type FlowInfoMsg,
  type RunSnapshotMsg,
} from "./model";
import { Inspector } from "./Inspector";

const declaration = JSON.stringify({
  signature: {
    name: "Analyze",
    instructions: "# Investigate\nUse **retained evidence**.",
    inputs: [{ name: "question", annotation: "str", description: "Question to answer" }],
    outputs: [{ name: "answer", annotation: "str", description: "Final answer" }],
  },
  runtime: { timeout: 30 },
  models: { main: "reasoning-model" },
  skills: [{ name: "Research", instructions: "Search **carefully**." }],
  tools: [{ name: "lookup", description: "Reads `trusted` sources." }],
});

const fieldSchemas = JSON.stringify({
  inputs: [{ name: "question", annotation: "str", description: "Question to answer" }],
  outputs: [{ name: "answer", annotation: "str", description: "Final answer" }],
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
  asOfEventUlid: "9",
  summary: {
    runId: "run-1",
    flowName: "agent_flow",
    status: "success",
    startedAt: 10,
    endedAt: 11,
    triggeredAt: 9,
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
        eventCount: "2",
        sizeBytes: "256",
        latestEventSequence: "2",
        header: {
          status: "completed",
          model: "main-model",
          subModel: "sub-model",
          iterations: "2",
          maxIterations: "4",
          durationMs: "125",
          usageJson: '{"input_tokens":12}',
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
    agentInstructionLines: { agent_1: "Answer the question." },
  },
};

function event(
  sequence: number,
  kind: AgentEventDescriptorMsg["eventKind"] = "iteration.recorded",
): AgentEventDescriptorMsg {
  return {
    eventSequence: String(sequence),
    sizeBytes: "64",
    bodyToken: `event-${sequence}`,
    invocationId: "invocation-1",
    eventKind: kind,
    iteration: kind === "iteration.recorded" ? sequence : undefined,
    durationMs: kind === "iteration.recorded" ? "10" : undefined,
    toolCount: kind === "iteration.recorded" ? 1 : 0,
    predictCount: kind === "iteration.recorded" ? 1 : 0,
    error: false,
  };
}

function eventPage(
  records: AgentEventDescriptorMsg[],
  nextPageToken = "",
  nextCursor = records.at(-1)?.eventSequence ?? "0",
): AgentEventDescriptorPage {
  return {
    operatorInstanceId: run.operatorInstanceId,
    asOfEventUlid: run.asOfEventUlid,
    runId: run.summary!.runId,
    nodeId: "agent_1",
    records,
    nextPageToken,
    nextCursor,
  };
}

function operatorApi(): OperatorApi {
  const events = [event(1, "run.started"), event(2, "run.succeeded")];
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
    listAgentEventPage: async () => eventPage(events),
    listLogPage: async () => ({
      operatorInstanceId: run.operatorInstanceId,
      asOfEventUlid: run.asOfEventUlid,
      records: [],
      nextPageToken: "",
      nextCursor: "0",
    }),
    readJsonDetail: async (token) =>
      token === "event-1"
        ? { inputs: { question: "Why?" } }
        : { outputs: { answer: "Because." } },
    readTextDetail: async (token) => token,
    startRun: async () => "unused",
    cancelRun: async () => undefined,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((next, fail) => {
    resolve = next;
    reject = fail;
  });
  return { promise, reject, resolve };
}

describe("Inspector", () => {
  it("renders declaration instructions, skills, and tools as Markdown without object dumps", () => {
    const view = render(
      <Inspector
        api={operatorApi()}
        workflow={workflow}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    expect(screen.getByRole("heading", { name: "Investigate" })).toBeInTheDocument();
    expect(screen.getByText("retained evidence").tagName).toBe("STRONG");
    expect(screen.getByText("Research")).toBeInTheDocument();
    expect(screen.getByText("carefully").tagName).toBe("STRONG");
    expect(screen.getByText("lookup")).toBeInTheDocument();
    expect(screen.getByText("trusted").tagName).toBe("CODE");
    expect(view.container).not.toHaveTextContent('"skills"');
    expect(view.container.querySelector(".inspector-body-full")).toBeInTheDocument();
  });

  it("keeps Overview hydration-free and gives the active panel the full-height classes", () => {
    const api = operatorApi();
    const listAgentEventPage = vi.fn(api.listAgentEventPage);
    const listLogPage = vi.fn(api.listLogPage);
    const readJsonDetail = vi.fn(api.readJsonDetail);
    const view = render(
      <Inspector
        api={{ ...api, listAgentEventPage, listLogPage, readJsonDetail }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    expect(listAgentEventPage).not.toHaveBeenCalled();
    expect(listLogPage).not.toHaveBeenCalled();
    expect(readJsonDetail).not.toHaveBeenCalled();
    expect(view.container.querySelector(".inspector-run")).toHaveClass("inspector");
    expect(view.container.querySelector(".inspector-body")).toHaveClass("inspector-body-full");
    expect(view.container.querySelector(".inspector-overview")).toHaveClass("inspector-panel");
    expect(screen.queryByText("Revision")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "logs" })).not.toBeInTheDocument();
  });

  it("shows immediate Inputs loading, cancels superseded detail, and renders the retained root directly", async () => {
    const inputDetail = deferred<unknown>();
    const outputDetail = deferred<unknown>();
    let inputSignal: AbortSignal | undefined;
    const readJsonDetail = vi.fn((token: string, signal?: AbortSignal) => {
      if (token === "event-1") {
        inputSignal = signal;
        return inputDetail.promise;
      }
      return outputDetail.promise;
    });
    render(
      <Inspector
        api={{ ...operatorApi(), readJsonDetail }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading retained inputs");
    await waitFor(() => expect(inputSignal).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(inputSignal!.aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent("Loading retained output");
    await act(async () => {
      outputDetail.resolve({ outputs: { answer: "fresh output" } });
      await outputDetail.promise;
    });
    const outputTree = await screen.findByRole("tree", { name: "JSON value" });
    expect(within(outputTree).getByText("fresh output")).toBeInTheDocument();
    expect(within(outputTree).getByText("answer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Object/i })).not.toBeInTheDocument();

    await act(async () => {
      inputDetail.resolve({ inputs: { question: "stale input" } });
      await inputDetail.promise;
    });
    expect(screen.queryByText("stale input")).not.toBeInTheDocument();
    expect(screen.getByText("fresh output")).toBeInTheDocument();
  });

  it("renders the empty Inputs copy only after an authoritative empty page", async () => {
    const api = operatorApi();
    const readJsonDetail = vi.fn(api.readJsonDetail);
    render(
      <Inspector
        api={{
          ...api,
          listAgentEventPage: async () => eventPage([]),
          readJsonDetail,
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading retained inputs");
    expect(await screen.findByText("No retained inputs are available.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(readJsonDetail).not.toHaveBeenCalled();
  });

  it("retains the Inputs run.started event while forward paging exceeds the descriptor window", async () => {
    const pages = Array.from({ length: 6 }, (_, pageIndex) =>
      Array.from({ length: 100 }, (_, index) => {
        const sequence = pageIndex * 100 + index + 1;
        return event(sequence, sequence === 1 ? "run.started" : "iteration.recorded");
      }),
    );
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      const pageIndex =
        request.pageToken === "events"
          ? 0
          : Number(request.pageToken.replace("events-", "")) - 1;
      return eventPage(
        pages[pageIndex],
        pageIndex < pages.length - 1 ? `events-${pageIndex + 2}` : "",
        String((pageIndex + 1) * 100),
      );
    });
    render(
      <Inspector
        api={{ ...operatorApi(), listAgentEventPage }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    expect(await screen.findByText("Why?")).toBeInTheDocument();
    for (let pageIndex = 1; pageIndex < pages.length; pageIndex += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
      await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(pageIndex + 1));
      if (pageIndex < pages.length - 1) {
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "Load more events" })).toBeEnabled(),
        );
      }
    }
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        afterEventSequence: "500",
        beforeEventSequence: "0",
        order: DescriptorPageOrder.FORWARD,
      }),
      expect.any(AbortSignal),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Load more events" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Why?")).toBeInTheDocument();
  });

  it("retains the terminal Output value event while backward paging exceeds the descriptor window", async () => {
    const pages = Array.from({ length: 6 }, (_, pageIndex) =>
      Array.from({ length: 100 }, (_, index) => {
        const sequence = 600 - pageIndex * 100 - index;
        return event(sequence, sequence === 600 ? "run.succeeded" : "iteration.recorded");
      }),
    );
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      const pageIndex =
        request.pageToken === "events"
          ? 0
          : Number(request.pageToken.replace("events-", "")) - 1;
      return eventPage(
        pages[pageIndex],
        pageIndex < pages.length - 1 ? `events-${pageIndex + 2}` : "",
        String(501 - pageIndex * 100),
      );
    });
    render(
      <Inspector
        api={{ ...operatorApi(), listAgentEventPage }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(await screen.findByText("Because.")).toBeInTheDocument();
    for (let pageIndex = 1; pageIndex < pages.length; pageIndex += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
      await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(pageIndex + 1));
      if (pageIndex < pages.length - 1) {
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "Load more events" })).toBeEnabled(),
        );
      }
    }
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        afterEventSequence: "0",
        beforeEventSequence: "101",
        order: DescriptorPageOrder.NEWEST_FIRST,
      }),
      expect.any(AbortSignal),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Load more events" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Because.")).toBeInTheDocument();
  });

  it("cancels an inactive descriptor page and suppresses its late completion", async () => {
    const pending = deferred<AgentEventDescriptorPage>();
    let pageSignal: AbortSignal | undefined;
    const api: OperatorApi = {
      ...operatorApi(),
      listAgentEventPage: (_request, signal) => {
        pageSignal = signal;
        return pending.promise;
      },
    };
    render(
      <Inspector
        api={api}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    expect(await screen.findByText("Loading retained trace…")).toBeInTheDocument();
    await waitFor(() => expect(pageSignal).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "overview" }));
    expect(pageSignal!.aborted).toBe(true);

    await act(async () => {
      pending.resolve(eventPage([event(99)]));
      await pending.promise;
    });
    expect(screen.queryByText("99 retained turns")).not.toBeInTheDocument();
  });

  it("projects trace header and progressively hydrated turns into one hierarchy", async () => {
    const firstTurnBody = deferred<unknown>();
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) =>
      request.pageToken === "events"
        ? eventPage([event(1), event(2)], "events-next", "2")
        : eventPage([event(3)], "", "3"),
    );
    const readJsonDetail = vi.fn((token: string) =>
      token === "event-1"
        ? firstTurnBody.promise
        : Promise.resolve({ reasoning: `reasoning-${token}`, output: `output-${token}` }),
    );
    const api = { ...operatorApi(), listAgentEventPage, readJsonDetail };
    const view = render(
      <Inspector
        api={api}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    const trace = await screen.findByRole("heading", { name: "RunTrace" });
    const panel = trace.closest("section")!;
    expect(within(panel).getByText("main-model")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Expand telemetry" }));
    expect(within(panel).getByText("trace_id")).toBeInTheDocument();
    expect(view.container.querySelector(".turn-list")).not.toBeInTheDocument();
    expect(view.container.querySelector(".turn-row")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Turn 1/ })).not.toBeInTheDocument();

    expect(await within(panel).findByText("2 retained turns")).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Expand turns" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(2));
    expect(listAgentEventPage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        pageToken: "events-next",
        afterEventSequence: "2",
        beforeEventSequence: "0",
        order: DescriptorPageOrder.FORWARD,
      }),
      expect.any(AbortSignal),
    );
    expect(await within(panel).findByRole("button", { name: "Expand 2" })).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Expand 0" }));
    expect(await within(panel).findByText("Loading retained turn…")).toBeInTheDocument();
    await act(async () => {
      firstTurnBody.resolve({ reasoning: "reasoning-one", code: "print('one')" });
      await firstTurnBody.promise;
    });
    expect(await within(panel).findByText("reasoning-one")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Collapse turns" })).toBeInTheDocument();

    view.rerender(
      <Inspector
        api={api}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        liveEvents={[event(4)]}
        onClose={() => undefined}
      />,
    );
    expect(within(panel).getByRole("button", { name: "Collapse 0" })).toBeInTheDocument();
    expect(within(panel).getByText("reasoning-one")).toBeInTheDocument();
    expect(await within(panel).findByRole("button", { name: "Expand 3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Following live" })).toBeInTheDocument();
  });

  it("aborts deferred trace detail hydration when the Inspector unmounts", async () => {
    const turnDetail = deferred<unknown>();
    let detailSignal: AbortSignal | undefined;
    const readJsonDetail = vi.fn((_token: string, signal?: AbortSignal) => {
      detailSignal = signal;
      return turnDetail.promise;
    });
    const view = render(
      <Inspector
        api={{
          ...operatorApi(),
          listAgentEventPage: async () => eventPage([event(1)]),
          readJsonDetail,
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    expect(await screen.findByText("1 retained turn")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand turns" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand 0" }));
    await waitFor(() => expect(detailSignal).toBeDefined());

    view.unmount();
    expect(detailSignal!.aborted).toBe(true);
    await act(async () => {
      turnDetail.resolve({ reasoning: "too late" });
      await turnDetail.promise;
    });
    expect(readJsonDetail).toHaveBeenCalledTimes(1);
  });

  it("evicts trace bodies by reported bytes and rejects an individually oversized body", async () => {
    const records = Array.from({ length: 6 }, (_, index) => ({
      ...event(index + 1),
      sizeBytes: index === 5 ? String(9 * 1024 * 1024) : String(2 * 1024 * 1024),
    }));
    const readJsonDetail = vi.fn(async (token: string) => ({ body: `body-${token}` }));
    render(
      <Inspector
        api={{
          ...operatorApi(),
          listAgentEventPage: async () => eventPage(records),
          readJsonDetail,
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    fireEvent.click(await screen.findByRole("button", { name: "Expand turns" }));
    for (let index = 0; index < 5; index += 1) {
      fireEvent.click(screen.getByRole("button", { name: `Expand ${index}` }));
      await waitFor(() =>
        expect(
          readJsonDetail.mock.calls.filter(([token]) => token === `event-${index + 1}`),
        ).toHaveLength(1),
      );
      expect(await screen.findByText(`body-event-${index + 1}`)).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: "Collapse 0" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand 0" }));
    await waitFor(() =>
      expect(readJsonDetail.mock.calls.filter(([token]) => token === "event-1")).toHaveLength(
        2,
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand 5" }));
    expect(
      await screen.findByText("Unavailable · Turn detail exceeds the browser detail limit."),
    ).toBeInTheDocument();
  });

  it("surfaces bounded trace page failures without mounting a selector", async () => {
    render(
      <Inspector
        api={{
          ...operatorApi(),
          listAgentEventPage: async () => {
            throw new Error("trace page failed");
          },
        }}
        workflow={workflow}
        run={run}
        nodeId="agent_1"
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("trace page failed");
    expect(document.querySelector(".turn-list")).not.toBeInTheDocument();
  });
});
