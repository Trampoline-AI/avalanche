import { Profiler, useEffect } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The production virtualizer is exercised by benchmark:browser. This unit volume
// gate isolates projection, paging, cache, and mounted-row bounds from jsdom layout.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 32,
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 120) }, (_, index) => ({
        index,
        size: 32,
        start: index * 32,
      })),
    scrollToIndex: () => undefined,
  }),
}));

import type { OperatorApi, StructuralBaseline } from "./api";
import { RunListPanel } from "./RunListPanel";
import {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  DescriptorPageOrder,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
  WorkflowTopologyMsg,
} from "./generated/operator";
import { Inspector } from "./Inspector";
import { RunLogPane } from "./RunLogPane";
import { useOperatorProjection } from "./state";

const OPERATOR_ID = "benchmark-operator";
const WORKFLOW_ID = "benchmark.py::large_run";
const RUN_ID = "run-09999";
const SELECTED_NODE = "node-042";
const RUN_SUMMARY_COUNT = 10_000;
const NODE_COUNT = 100;
const LOGS_PER_NODE = 500;
const LOG_DESCRIPTOR_COUNT = NODE_COUNT * LOGS_PER_NODE;
const AGENT_EVENT_COUNT = 20_000;
const ENVELOPE_COUNT = 1_000;
const PAGE_SIZE = 100;
const DOM_ROW_LIMIT = 120;
const LARGE_DETAIL_BYTES = 2 * 1024 * 1024;
const UNIT_RENDER_BUDGET_MS = 10_000;

function summary(index: number) {
  return RunSummaryMsg.create({
    runId: `run-${index.toString().padStart(5, "0")}`,
    workflowId: WORKFLOW_ID,
    workflowDisplayName: "Benchmark flow",
    status: index % 7 === 0 ? "failed" : "success",
    startedAt: index + 1,
    endedAt: index + 2,
    createdSequence: String(index + 1),
    revision: String(index + 1),
  });
}

const runSummaries = Array.from({ length: RUN_SUMMARY_COUNT }, (_, index) => summary(index));
const catalog = CatalogSnapshotMsg.create({
  operatorInstanceId: OPERATOR_ID,
  asOfSequence: "0",
  revision: "1",
  scanTargets: [{ alias: "bench", targetPath: "/controlled/bench", kind: "directory" }],
  workflows: [
    {
      workflowId: WORKFLOW_ID,
      displayName: "Benchmark flow",
      rootAlias: "bench",
      relativeFile: "benchmark.py",
      builderSymbol: "large_run",
      nodeIds: Array.from(
        { length: NODE_COUNT },
        (_, index) => `node-${index.toString().padStart(3, "0")}`,
      ),
      graph: {},
      nodeTypes: {},
      displayNames: {},
      agentNodeIds: [SELECTED_NODE],
      agentMetadataJson: {},
    },
  ],
});
const baseline: StructuralBaseline = { catalog, asOfSequence: "0", runs: runSummaries };

const topology = WorkflowTopologyMsg.create({
  nodeIds: catalog.workflows[0].nodeIds,
  graph: Object.fromEntries(
    catalog.workflows[0].nodeIds.map((nodeId) => [nodeId, { children: [] }]),
  ),
  nodeTypes: Object.fromEntries(catalog.workflows[0].nodeIds.map((nodeId) => [nodeId, "task"])),
  displayNames: Object.fromEntries(
    catalog.workflows[0].nodeIds.map((nodeId) => [nodeId, `Benchmark ${nodeId}`]),
  ),
});
const selectedRun = RunSnapshotMsg.create({
  operatorInstanceId: OPERATOR_ID,
  asOfSequence: "0",
  summary: runSummaries.at(-1),
  topology,
  nodes: catalog.workflows[0].nodeIds.map((nodeId) => ({
    nodeId,
    name: nodeId,
    nodeType: "task",
    status: "success",
    startedAt: 1,
    endedAt: 2,
    revision: "1",
    eventPageToken: nodeId === SELECTED_NODE ? "events:0" : "",
  })),
  latestLogSequence: String(LOG_DESCRIPTOR_COUNT),
  logPageToken: "logs:0",
});

const logDescriptors = Array.from({ length: LOG_DESCRIPTOR_COUNT }, (_, index) => {
  const nodeIndex = Math.floor(index / LOGS_PER_NODE);
  const nodeId = `node-${nodeIndex.toString().padStart(3, "0")}`;
  return LogRecordDescriptorMsg.create({
    sequence: String(index + 1),
    nodeId,
    timestamp: index + 1,
    level: index % 11 === 0 ? "warning" : "info",
    sizeBytes: "64",
    bodyToken: `log:${nodeId}:${index + 1}`,
  });
});
const agentEvents = Array.from({ length: AGENT_EVENT_COUNT }, (_, index) =>
  AgentEventDescriptorMsg.create({
    eventSequence: String(index + 1),
    sizeBytes: String(LARGE_DETAIL_BYTES),
    bodyToken:
      index === 0
        ? "detail:input"
        : index === AGENT_EVENT_COUNT - 1
          ? "detail:output"
          : `event:${index + 1}`,
    invocationId: `invocation-${Math.floor(index / 10)}`,
    eventKind:
      index === 0
        ? "run.started"
        : index === AGENT_EVENT_COUNT - 1
          ? "run.succeeded"
          : "iteration.recorded",
    iteration: index > 0 && index < AGENT_EVENT_COUNT - 1 ? index : 0,
    durationMs: "1",
    toolCount: index % 3,
    predictCount: index % 2,
  }),
);

function deferred<T>() {
  return Promise.withResolvers<T>();
}

function untilAborted(signal?: AbortSignal) {
  const { promise, resolve } = Promise.withResolvers<void>();
  if (!signal || signal.aborted) {
    resolve();
  } else {
    signal.addEventListener("abort", () => resolve(), { once: true });
  }
  return promise;
}

class ManualFrameScheduler {
  private nextId = 1;
  readonly callbacks = new Map<number, FrameRequestCallback>();

  install() {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      const id = this.nextId;
      this.nextId += 1;
      this.callbacks.set(id, callback);
      return id;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      this.callbacks.delete(id);
    });
  }

  async flushOne() {
    const entry = this.callbacks.entries().next().value as
      [number, FrameRequestCallback] | undefined;
    if (!entry) throw new Error("No scheduled animation frame");
    this.callbacks.delete(entry[0]);
    await act(async () => {
      entry[1](this.nextId * 16);
      await Promise.resolve();
    });
  }
}

function liveEnvelope(sequence: number) {
  const descriptorSequence = String(Math.floor((sequence + 1) / 2));
  const change =
    sequence % 2 === 1
      ? {
          oneofKind: "logAppended" as const,
          logAppended: {
            runId: RUN_ID,
            log: LogRecordDescriptorMsg.create({
              sequence: descriptorSequence,
              nodeId: SELECTED_NODE,
              timestamp: sequence,
              level: "info",
              sizeBytes: "32",
              bodyToken: `live-log:${descriptorSequence}`,
            }),
          },
        }
      : {
          oneofKind: "agentEventAppended" as const,
          agentEventAppended: {
            runId: RUN_ID,
            nodeId: SELECTED_NODE,
            event: AgentEventDescriptorMsg.create({
              eventSequence: descriptorSequence,
              sizeBytes: "32",
              bodyToken: `live-event:${descriptorSequence}`,
              eventKind: "iteration.recorded",
              invocationId: "live",
            }),
          },
        };
  return OperatorUpdateEnvelope.create({
    operatorInstanceId: OPERATOR_ID,
    payload: {
      oneofKind: "update",
      update: { sequence: String(sequence), change },
    },
  });
}

function ProjectionProbe({
  api,
  observedSequences,
}: {
  api: OperatorApi;
  observedSequences: string[];
}) {
  const projection = useOperatorProjection(api);
  const { state } = projection;
  useEffect(() => {
    observedSequences.push(state.sequence);
  }, [observedSequences, state.sequence]);
  return (
    <section aria-label="Projection benchmark probe">
      <button type="button" onClick={() => void projection.selectRun(RUN_ID)}>
        Select benchmark run
      </button>
      <output data-testid="run-count">{Object.keys(state.runs).length}</output>
      <output data-testid="projection-sequence">{state.sequence}</output>
      <output data-testid="live-log-count">{state.liveLogs[RUN_ID]?.length ?? 0}</output>
      <output data-testid="live-event-count">
        {state.liveEvents[`${RUN_ID}:${SELECTED_NODE}`]?.length ?? 0}
      </output>
    </section>
  );
}

function decodedSplitText() {
  const expected = "plain text A😀B: not JSON }";
  const encoded = new TextEncoder().encode(expected);
  const splitAt = encoded.indexOf(0xf0) + 2;
  const decoder = new TextDecoder();
  return (
    decoder.decode(encoded.slice(0, splitAt), { stream: true }) +
    decoder.decode(encoded.slice(splitAt))
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("large retained-run browser benchmark", () => {
  it("bounds summary DOM and frame-batches 1,000 ordered live envelopes", async () => {
    expect(runSummaries).toHaveLength(RUN_SUMMARY_COUNT);
    const scheduler = new ManualFrameScheduler();
    scheduler.install();
    const startUpdates = deferred<void>();
    let yieldedUpdates = 0;
    const getLatestRunSnapshot = vi.fn(async () =>
      RunSnapshotMsg.create({
        ...selectedRun,
        asOfSequence: String(yieldedUpdates),
      }),
    );
    const api: OperatorApi = {
      getCatalog: async () => catalog,
      loadBaseline: vi.fn(async () => baseline),
      getLatestRunSnapshot,
      streamUpdates: async function* (_operatorInstanceId, _afterSequence, signal) {
        await startUpdates.promise;
        for (let sequence = 1; sequence <= ENVELOPE_COUNT; sequence += 1) {
          yieldedUpdates += 1;
          yield liveEnvelope(sequence);
        }
        await untilAborted(signal);
      },
      listLogPage: async () => {
        throw new Error("unused");
      },
      listAgentEventPage: async () => {
        throw new Error("unused");
      },
      readJsonDetail: async () => {
        throw new Error("unused");
      },
      readTextDetail: async () => {
        throw new Error("unused");
      },
      startRun: async () => RUN_ID,
      cancelRun: async () => undefined,
    };
    const observedSequences: string[] = [];
    const projectionView = render(
      <ProjectionProbe api={api} observedSequences={observedSequences} />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("run-count")).toHaveTextContent(String(RUN_SUMMARY_COUNT)),
    );
    expect(getLatestRunSnapshot).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Select benchmark run" }));
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(1));
    startUpdates.resolve();
    await waitFor(() => expect(yieldedUpdates).toBe(ENVELOPE_COUNT));
    await waitFor(() => expect(scheduler.callbacks.size).toBe(1));

    expect(screen.getByTestId("projection-sequence")).toHaveTextContent("0");
    while (scheduler.callbacks.size > 0) await scheduler.flushOne();

    await waitFor(() =>
      expect(screen.getByTestId("projection-sequence")).toHaveTextContent(
        String(ENVELOPE_COUNT),
      ),
    );
    expect(Number(screen.getByTestId("live-log-count").textContent)).toBeLessThanOrEqual(256);
    expect(Number(screen.getByTestId("live-event-count").textContent)).toBeLessThanOrEqual(256);
    const committedSequences = observedSequences
      .map(Number)
      .filter((value, index, values) => index === 0 || value !== values[index - 1]);
    const frameDeltas = committedSequences
      .slice(1)
      .map((value, index) => value - committedSequences[index])
      .filter((delta) => delta > 0);
    expect(frameDeltas.length).toBeGreaterThan(1);
    expect(Math.max(...frameDeltas)).toBeLessThanOrEqual(256);
    projectionView.unmount();

    const onSelectRun = vi.fn();
    const runListRenderStart = performance.now();
    const runListView = render(
      <RunListPanel
        workflowId={WORKFLOW_ID}
        runs={Object.fromEntries(runSummaries.map((item) => [item.runId, item]))}
        onSelectRun={onSelectRun}
      />,
    );
    const virtualRunList = await within(runListView.container).findByRole("region", {
      name: "Workflow runs",
    });
    const newestRun = await within(virtualRunList).findByRole("button", {
      name: /run-09999/,
    });
    expect(virtualRunList.querySelectorAll(".run-list-row").length).toBeLessThanOrEqual(
      DOM_ROW_LIMIT,
    );
    expect(performance.now() - runListRenderStart).toBeLessThan(UNIT_RENDER_BUDGET_MS);
    fireEvent.click(newestRun);
    expect(onSelectRun).toHaveBeenLastCalledWith(RUN_ID);
  }, 30_000);

  it("pages controlled descriptor volumes and suppresses stale large details", async () => {
    expect(logDescriptors).toHaveLength(LOG_DESCRIPTOR_COUNT);
    expect(new Set(logDescriptors.map((entry) => entry.nodeId)).size).toBe(NODE_COUNT);
    expect(agentEvents).toHaveLength(AGENT_EVENT_COUNT);
    const staleInput = deferred<unknown>();
    const freshOutput = deferred<unknown>();
    const largePayload = "x".repeat(LARGE_DETAIL_BYTES);
    const selectedLogs = logDescriptors.filter((entry) => entry.nodeId === SELECTED_NODE);
    const listAgentEventPage = vi.fn<OperatorApi["listAgentEventPage"]>(async (request) => {
      if (request.order === DescriptorPageOrder.NEWEST_FIRST) {
        const upper =
          request.beforeEventSequence === "0"
            ? agentEvents.length
            : Number(request.beforeEventSequence) - 1;
        const lower = Math.max(1, upper - request.pageSize + 1);
        return {
          operatorInstanceId: OPERATOR_ID,
          asOfSequence: selectedRun.asOfSequence,
          runId: RUN_ID,
          nodeId: SELECTED_NODE,
          records: agentEvents.slice(lower - 1, upper).reverse(),
          nextPageToken: lower === 1 ? "" : "events:older",
          nextCursor: String(lower),
        };
      }
      const start = Number(request.afterEventSequence);
      const records = agentEvents.slice(start, start + request.pageSize);
      return {
        operatorInstanceId: OPERATOR_ID,
        asOfSequence: selectedRun.asOfSequence,
        runId: RUN_ID,
        nodeId: SELECTED_NODE,
        records,
        nextPageToken: start + records.length >= agentEvents.length ? "" : "events:next",
        nextCursor: records.at(-1)?.eventSequence ?? request.afterEventSequence,
      };
    });
    const listLogPage = vi.fn<OperatorApi["listLogPage"]>(async (request) => {
      const end =
        request.beforeSequence === "0"
          ? selectedLogs.length
          : selectedLogs.findIndex((entry) => entry.sequence === request.beforeSequence);
      const records = selectedLogs.slice(Math.max(0, end - request.pageSize), end).reverse();
      return {
        operatorInstanceId: OPERATOR_ID,
        asOfSequence: selectedRun.asOfSequence,
        records,
        nextPageToken: end === selectedLogs.length ? "logs:next" : "logs:next-2",
        nextCursor: records.at(-1)?.sequence ?? request.beforeSequence,
      };
    });
    const readJsonDetail = vi.fn<OperatorApi["readJsonDetail"]>((bodyToken) => {
      if (bodyToken === "detail:input") return staleInput.promise;
      if (bodyToken === "detail:output") return freshOutput.promise;
      return Promise.resolve({ event: bodyToken, payload: largePayload });
    });
    const readTextDetail = vi.fn<OperatorApi["readTextDetail"]>(async () => decodedSplitText());
    const api: OperatorApi = {
      getCatalog: async () => catalog,
      loadBaseline: async () => baseline,
      getLatestRunSnapshot: async () => selectedRun,
      streamUpdates: async function* () {
        return;
      },
      listAgentEventPage,
      listLogPage,
      readJsonDetail,
      readTextDetail,
      startRun: async () => RUN_ID,
      cancelRun: async () => undefined,
    };
    let inspectorCommits = 0;
    const benchmarkStart = performance.now();
    render(
      <Profiler
        id="large-inspector"
        onRender={() => {
          inspectorCommits += 1;
        }}
      >
        <Inspector
          api={api}
          workflow={catalog.workflows[0]}
          run={selectedRun}
          nodeId={SELECTED_NODE}
          onClose={() => undefined}
        />
      </Profiler>,
    );

    expect(listAgentEventPage).not.toHaveBeenCalled();
    expect(listLogPage).not.toHaveBeenCalled();
    expect(readJsonDetail).not.toHaveBeenCalled();
    expect(readTextDetail).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    await waitFor(() =>
      expect(readJsonDetail.mock.calls.some(([token]) => token === "detail:input")).toBe(true),
    );
    expect(listAgentEventPage).toHaveBeenCalledTimes(1);
    expect(listAgentEventPage.mock.calls[0][0]).toMatchObject({
      pageToken: "events:0",
      afterEventSequence: "0",
      pageSize: PAGE_SIZE,
      order: DescriptorPageOrder.FORWARD,
      expectedNodeId: SELECTED_NODE,
    });

    fireEvent.click(screen.getByRole("button", { name: "output" }));
    await waitFor(() =>
      expect(readJsonDetail.mock.calls.some(([token]) => token === "detail:output")).toBe(true),
    );
    expect(listAgentEventPage).toHaveBeenCalledTimes(2);
    expect(listAgentEventPage.mock.calls[1][0]).toMatchObject({
      beforeEventSequence: "0",
      order: DescriptorPageOrder.NEWEST_FIRST,
      expectedNodeId: SELECTED_NODE,
    });
    const outputDetail = {
      data: { outputs: { freshMarker: "fresh-detail", payload: largePayload } },
    };
    expect(JSON.stringify(outputDetail).length).toBeGreaterThanOrEqual(LARGE_DETAIL_BYTES);
    await act(async () => {
      freshOutput.resolve(outputDetail);
      await Promise.resolve();
    });
    const outputTree = await screen.findByRole("tree", { name: "JSON value" });
    expect(within(outputTree).getByText("fresh-detail")).toBeInTheDocument();
    expect(within(outputTree).getByText("freshMarker")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Object/ })).not.toBeInTheDocument();

    const commitsBeforeStaleCompletion = inspectorCommits;
    await act(async () => {
      staleInput.resolve({ data: { inputs: { staleMarker: "stale-detail" } } });
      await Promise.resolve();
    });
    expect(inspectorCommits).toBe(commitsBeforeStaleCompletion);
    expect(screen.queryByText("stale-detail")).not.toBeInTheDocument();
    expect(screen.getByText("fresh-detail")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "inputs" }));
    const inputTree = await screen.findByRole("tree", { name: "JSON value" });
    expect(listAgentEventPage).toHaveBeenCalledTimes(3);
    expect(within(inputTree).getByText("stale-detail")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Object/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
    await waitFor(() => {
      expect(listAgentEventPage).toHaveBeenCalledTimes(4);
      expect(screen.getByRole("button", { name: "Load more events" })).not.toBeDisabled();
    });
    expect(listAgentEventPage).toHaveBeenCalledTimes(4);
    expect(listAgentEventPage.mock.calls[3][0]).toMatchObject({
      pageToken: "events:next",
      afterEventSequence: String(PAGE_SIZE),
      pageSize: PAGE_SIZE,
      order: DescriptorPageOrder.FORWARD,
      expectedNodeId: SELECTED_NODE,
    });

    fireEvent.click(screen.getByRole("button", { name: "trace" }));
    expect(await screen.findByText("99 retained turns")).toBeInTheDocument();
    expect(listAgentEventPage).toHaveBeenCalledTimes(5);
    expect(listAgentEventPage.mock.calls[4][0]).toMatchObject({
      pageToken: "events:0",
      afterEventSequence: "0",
      pageSize: PAGE_SIZE,
      order: DescriptorPageOrder.FORWARD,
      expectedNodeId: SELECTED_NODE,
    });
    fireEvent.click(screen.getByRole("button", { name: "Expand turns" }));
    await waitFor(() => expect(listAgentEventPage).toHaveBeenCalledTimes(6));
    expect(listAgentEventPage.mock.calls[5][0]).toMatchObject({
      pageToken: "events:next",
      afterEventSequence: String(PAGE_SIZE),
      pageSize: PAGE_SIZE,
      order: DescriptorPageOrder.FORWARD,
      expectedNodeId: SELECTED_NODE,
    });
    fireEvent.click(screen.getByRole("button", { name: "Following live" }));
    for (let turn = 0; turn < 5; turn += 1) {
      const token = `event:${turn + 2}`;
      fireEvent.click(await screen.findByRole("button", { name: `Expand ${turn}` }));
      await waitFor(() =>
        expect(readJsonDetail.mock.calls.filter(([called]) => called === token)).toHaveLength(
          1,
        ),
      );
      expect(await screen.findByText(token)).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("button", { name: "Collapse 0" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand 0" }));
    await waitFor(() =>
      expect(readJsonDetail.mock.calls.filter(([token]) => token === "event:2")).toHaveLength(
        2,
      ),
    );

    const logPaneView = render(
      <RunLogPane
        api={api}
        run={selectedRun}
        nodeId={SELECTED_NODE}
        onSelectNode={() => undefined}
      />,
    );
    const logPane = await within(logPaneView.container).findByRole("region", {
      name: "Run logs",
    });
    await waitFor(() => expect(listLogPage).toHaveBeenCalledTimes(1));
    expect(listLogPage.mock.calls[0][0]).toMatchObject({
      pageSize: PAGE_SIZE,
      nodeId: SELECTED_NODE,
      order: DescriptorPageOrder.NEWEST_FIRST,
    });
    await waitFor(() => expect(readTextDetail).toHaveBeenCalledTimes(PAGE_SIZE));
    const initialLogRows = logPane.querySelectorAll(".run-log-row");
    expect(initialLogRows).toHaveLength(PAGE_SIZE);
    expect(
      Array.from(initialLogRows).every(
        (row) => row.querySelector("pre")?.textContent === decodedSplitText(),
      ),
    ).toBe(true);
    expect(document.querySelector(".inspector-log-stream")).not.toBeInTheDocument();

    fireEvent.click(within(logPane).getByRole("button", { name: "Load older logs" }));
    await waitFor(() => expect(listLogPage).toHaveBeenCalledTimes(2));
    expect(listLogPage.mock.calls[1][0]).toMatchObject({
      pageToken: "logs:next",
      beforeSequence: selectedLogs.at(-PAGE_SIZE)?.sequence,
      pageSize: PAGE_SIZE,
      nodeId: SELECTED_NODE,
      order: DescriptorPageOrder.NEWEST_FIRST,
    });
    await waitFor(() => expect(readTextDetail).toHaveBeenCalledTimes(PAGE_SIZE * 2));
    expect(logPane.querySelectorAll(".run-log-row").length).toBeLessThanOrEqual(DOM_ROW_LIMIT);
    expect(listLogPage).toHaveBeenCalledTimes(2);
    expect(performance.now() - benchmarkStart).toBeLessThan(30_000);
  }, 30_000);
});
