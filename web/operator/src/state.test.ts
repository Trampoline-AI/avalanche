import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OperatorApi, StructuralBaseline } from "./api";
import {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  FlowInfoMsg,
  LogRecordDescriptorMsg,
  NodeSnapshotMsg,
  type OperatorUpdate,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
  TraceDescriptorMsg,
  WorkflowTopologyMsg,
} from "./model";
import { emptyProjection, projectionReducer, useOperatorProjection } from "./state";

function eventUlid(sequence: number | string): string {
  return Number(sequence).toString(16).toUpperCase().padStart(26, "0");
}

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
const secondSummary = RunSummaryMsg.create({
  ...summary,
  runId: "run-2",
  createdSequence: "2",
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
    asOfEventUlid: eventUlid(1),
    revision: "1",
    workflows: [workflow],
  }),
  asOfEventUlid: eventUlid(1),
  runs: [summary],
};

function snapshotFor(run: RunSummaryMsg): RunSnapshotMsg {
  return RunSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfEventUlid: eventUlid(1),
    summary: run,
    nodes: [node],
    topology,
  });
}

function envelope(
  sequence: number | string,
  change: OperatorUpdate["change"] = { oneofKind: undefined },
  operatorInstanceId = "operator-1",
): OperatorUpdateEnvelope {
  return OperatorUpdateEnvelope.create({
    operatorInstanceId,
    payload: { oneofKind: "update", update: { eventUlid: eventUlid(sequence), change } },
  });
}

function selectedState(run = summary) {
  const state = projectionReducer(emptyProjection, {
    type: "baseline",
    baseline: { ...baseline, runs: [summary, secondSummary] },
  });
  const loading = projectionReducer(state, {
    type: "selectionLoading",
    runId: run.runId,
  });
  return projectionReducer(loading, {
    type: "selectionReady",
    runId: run.runId,
    snapshot: snapshotFor(run),
  });
}

async function* idleUpdates(signal?: AbortSignal): AsyncIterable<OperatorUpdateEnvelope> {
  await new Promise<void>((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    signal?.addEventListener("abort", () => resolve(), { once: true });
  });
}

type ProjectionApi = Pick<
  OperatorApi,
  "loadBaseline" | "getLatestRunSnapshot" | "streamUpdates" | "startRun" | "cancelRun"
>;

function createApi(overrides: Partial<ProjectionApi> = {}): OperatorApi {
  const defaults: ProjectionApi = {
    loadBaseline: async () => baseline,
    getLatestRunSnapshot: async (runId) =>
      snapshotFor(runId === secondSummary.runId ? secondSummary : summary),
    streamUpdates: (_operatorInstanceId, _afterSequence, signal) => idleUpdates(signal),
    startRun: async () => "run-3",
    cancelRun: async () => undefined,
  };
  return { ...defaults, ...overrides } as OperatorApi;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("projectionReducer", () => {
  it("tracks workflow reload status updates", () => {
    const state = projectionReducer(emptyProjection, { type: "baseline", baseline });
    const reloading = projectionReducer(state, {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "workflowReloadStatus",
          workflowReloadStatus: { reloading: true },
        }),
      ],
    });
    const finished = projectionReducer(reloading, {
      type: "envelopes",
      envelopes: [
        envelope("3", {
          oneofKind: "workflowReloadStatus",
          workflowReloadStatus: { reloading: false },
        }),
      ],
    });

    expect(reloading.workflowReloading).toBe(true);
    expect(finished.workflowReloading).toBe(false);
  });

  it("installs summary-only baselines and clears selected ephemeral state", () => {
    const previous = {
      ...selectedState(),
      liveLogs: { "run-1": [LogRecordDescriptorMsg.create({ sequence: "1" })] },
      liveEvents: {
        "run-1:fetch": [AgentEventDescriptorMsg.create({ eventSequence: "1" })],
      },
      liveLogRepairWatermarks: { "run-1": "1" },
      liveEventRepairWatermarks: { "run-1:fetch": "1" },
    };

    const state = projectionReducer(previous, { type: "baseline", baseline });

    expect(state.runs).toEqual({ "run-1": summary });
    expect(state.runs[summary.runId]).not.toHaveProperty("nodes");
    expect(state.selectedRun).toBeUndefined();
    expect(state.selectedRunStatus).toBe("idle");
    expect(state.liveLogs).toEqual({});
    expect(state.liveEvents).toEqual({});
    expect(state.liveLogRepairWatermarks).toEqual({});
    expect(state.liveEventRepairWatermarks).toEqual({});
  });

  it("applies status to the summary and selected snapshot and detail only to the selection", () => {
    let state = selectedState();
    state = projectionReducer(state, {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: summary.runId,
            status: "failed",
            startedAt: 10,
            endedAt: 12,
            revision: "2",
          },
        }),
        envelope("3", {
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
        }),
        envelope("4", {
          oneofKind: "traceFinalized",
          traceFinalized: {
            runId: summary.runId,
            nodeId: "fetch",
            trace: TraceDescriptorMsg.create({ status: "failed", revision: "2" }),
          },
        }),
      ],
    });

    expect(state.runs[summary.runId]).toMatchObject({ status: "failed", revision: "2" });
    expect(state.selectedRun?.summary).toMatchObject({ status: "failed", revision: "2" });
    expect(state.selectedRun?.nodes[0]).toMatchObject({
      status: "failed",
      error: "source unavailable",
      trace: { status: "failed" },
    });
  });

  it("ignores node, trace, log, and event detail for an unselected run", () => {
    const original = selectedState();
    const state = projectionReducer(original, {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: secondSummary.runId,
            nodeId: "fetch",
            status: "failed",
            revision: "2",
            startedAt: 1,
            endedAt: 2,
          },
        }),
        envelope("3", {
          oneofKind: "traceFinalized",
          traceFinalized: {
            runId: secondSummary.runId,
            nodeId: "fetch",
            trace: TraceDescriptorMsg.create({ status: "complete", revision: "2" }),
          },
        }),
        envelope("4", {
          oneofKind: "logAppended",
          logAppended: {
            runId: secondSummary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "1", nodeId: "fetch" }),
          },
        }),
        envelope("5", {
          oneofKind: "agentEventAppended",
          agentEventAppended: {
            runId: secondSummary.runId,
            nodeId: "fetch",
            event: AgentEventDescriptorMsg.create({ eventSequence: "1" }),
          },
        }),
        envelope("6", {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: secondSummary.runId,
            status: "failed",
            revision: "2",
            startedAt: 1,
            endedAt: 2,
          },
        }),
      ],
    });

    expect(state.runs[secondSummary.runId].status).toBe("failed");
    expect(state.selectedRun?.nodes).toEqual(original.selectedRun?.nodes);
    expect(state.selectedRun?.summary?.status).toBe("running");
    expect(state.liveLogs).toEqual({});
    expect(state.liveEvents).toEqual({});
  });

  it("orders and deduplicates one cross-step live descriptor tail per run", () => {
    const state = projectionReducer(selectedState(), {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "logAppended",
          logAppended: {
            runId: summary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "3", nodeId: "fetch" }),
          },
        }),
        envelope("3", {
          oneofKind: "logAppended",
          logAppended: {
            runId: summary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "1", nodeId: "fetch" }),
          },
        }),
        envelope("4", {
          oneofKind: "logAppended",
          logAppended: {
            runId: summary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "2", nodeId: "validate" }),
          },
        }),
        envelope("5", {
          oneofKind: "logAppended",
          logAppended: {
            runId: summary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "2", nodeId: "validate" }),
          },
        }),
      ],
    });

    expect(state.liveLogs["run-1"].map((entry) => entry.sequence)).toEqual(["1", "2", "3"]);
  });

  it("bounds live log and event tails and records repair watermarks", () => {
    const logEnvelopes = Array.from({ length: 260 }, (_, index) =>
      envelope(String(index + 2), {
        oneofKind: "logAppended",
        logAppended: {
          runId: summary.runId,
          log: LogRecordDescriptorMsg.create({ sequence: String(index + 1), nodeId: "fetch" }),
        },
      }),
    );
    const eventEnvelopes = Array.from({ length: 260 }, (_, index) =>
      envelope(String(index + 262), {
        oneofKind: "agentEventAppended",
        agentEventAppended: {
          runId: summary.runId,
          nodeId: "fetch",
          event: AgentEventDescriptorMsg.create({ eventSequence: String(index + 1) }),
        },
      }),
    );

    const state = projectionReducer(selectedState(), {
      type: "envelopes",
      envelopes: [...logEnvelopes, ...eventEnvelopes],
    });

    expect(state.liveLogs["run-1"]).toHaveLength(256);
    expect(state.liveLogs["run-1"][0].sequence).toBe("5");
    expect(state.liveLogRepairWatermarks["run-1"]).toBe("4");
    expect(state.liveEvents["run-1:fetch"]).toHaveLength(256);
    expect(state.liveEvents["run-1:fetch"][0].eventSequence).toBe("5");
    expect(state.liveEventRepairWatermarks["run-1:fetch"]).toBe("4");
  });

  it("installs summary, nodes, and continuations from one atomic snapshot revision", () => {
    const atomicSummary = RunSummaryMsg.create({
      ...summary,
      status: "complete",
      revision: "5",
    });
    const atomicSnapshot = RunSnapshotMsg.create({
      operatorInstanceId: "operator-1",
      asOfEventUlid: eventUlid(5),
      summary: atomicSummary,
      nodes: [
        NodeSnapshotMsg.create({
          ...node,
          status: "complete",
          revision: "5",
          eventPageToken: "events-r5",
        }),
      ],
      topology,
      logPageToken: "logs-r5",
    });
    const previous = {
      ...selectedState(),
      liveLogs: {
        "run-1": [LogRecordDescriptorMsg.create({ sequence: "2" })],
        "run-2": [LogRecordDescriptorMsg.create({ sequence: "9" })],
      },
      liveEvents: {
        "run-1:fetch": [AgentEventDescriptorMsg.create({ eventSequence: "2" })],
      },
      liveLogRepairWatermarks: { "run-1": "2", "run-2": "9" },
      liveEventRepairWatermarks: { "run-1:fetch": "2" },
    };

    let state = projectionReducer(previous, {
      type: "selectionReady",
      runId: summary.runId,
      snapshot: atomicSnapshot,
    });
    state = projectionReducer(state, {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: summary.runId,
            nodeId: node.nodeId,
            status: "running",
            revision: "2",
            startedAt: 1,
            endedAt: 0,
          },
        }),
        envelope("3", {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: summary.runId,
            status: "running",
            revision: "2",
            startedAt: 1,
            endedAt: 0,
          },
        }),
      ],
    });

    expect(state.selectedRun).toBe(atomicSnapshot);
    expect(state.selectedRun?.summary).toEqual(atomicSummary);
    expect(state.selectedRun?.nodes[0]).toMatchObject({
      revision: "5",
      eventPageToken: "events-r5",
    });
    expect(state.selectedRun?.logPageToken).toBe("logs-r5");
    expect(state.liveLogs["run-1"]).toBeUndefined();
    expect(state.liveEvents["run-1:fetch"]).toBeUndefined();
    expect(state.liveLogRepairWatermarks).toEqual({ "run-2": "9" });
    expect(state.liveEventRepairWatermarks).toEqual({});
  });

  it("rejects a snapshot overtaken by projection sequence or summary revision", () => {
    const initial = projectionReducer(emptyProjection, { type: "baseline", baseline });
    let loading = projectionReducer(initial, {
      type: "selectionLoading",
      runId: summary.runId,
    });
    loading = projectionReducer(loading, {
      type: "envelopes",
      envelopes: [
        envelope("2", {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: summary.runId,
            status: "complete",
            revision: "2",
            startedAt: 1,
            endedAt: 2,
          },
        }),
      ],
    });

    const sequenceStale = projectionReducer(loading, {
      type: "selectionReady",
      runId: summary.runId,
      snapshot: snapshotFor(summary),
    });
    const revisionStale = projectionReducer(loading, {
      type: "selectionReady",
      runId: summary.runId,
      snapshot: RunSnapshotMsg.create({
        ...snapshotFor(summary),
        asOfEventUlid: eventUlid(2),
      }),
    });

    expect(sequenceStale).toBe(loading);
    expect(revisionStale).toBe(loading);
    expect(loading.selectedRunStatus).toBe("loading");
    expect(loading.selectedRun).toBeUndefined();
  });

  it("rejects epoch changes, sequence gaps, and reset notices", () => {
    const state = projectionReducer(emptyProjection, { type: "baseline", baseline });
    const reset = OperatorUpdateEnvelope.create({
      operatorInstanceId: "operator-1",
      payload: {
        oneofKind: "resetRequired",
        resetRequired: {
          historyFloorEventUlid: eventUlid(2),
          latestEventUlid: eventUlid(8),
        },
      },
    });

    expect(() =>
      projectionReducer(state, {
        type: "envelopes",
        envelopes: [envelope("2", undefined, "other")],
      }),
    ).toThrow("epoch changed");
    expect(() =>
      projectionReducer(state, { type: "envelopes", envelopes: [envelope("3")] }),
    ).not.toThrow();
    expect(() => projectionReducer(state, { type: "envelopes", envelopes: [reset] })).toThrow(
      "structural reset",
    );
  });

  it("rejects an unknown run update before advancing the projection cursor", () => {
    const state = projectionReducer(emptyProjection, { type: "baseline", baseline });

    expect(() =>
      projectionReducer(state, {
        type: "envelopes",
        envelopes: [
          envelope("2", {
            oneofKind: "runStatusChanged",
            runStatusChanged: {
              runId: "run-missing",
              status: "failed",
              startedAt: 0,
              endedAt: 0,
              revision: "1",
            },
          }),
        ],
      }),
    ).toThrow("unknown run");
    expect(state.eventUlid).toBe(eventUlid(1));
  });

  it("keeps run creation and its following status update live", () => {
    const created = RunSummaryMsg.create({
      ...summary,
      runId: "run-created",
      status: "pending",
      createdSequence: "2",
      revision: "1",
    });
    const state = projectionReducer(
      projectionReducer(emptyProjection, { type: "baseline", baseline }),
      {
        type: "envelopes",
        envelopes: [
          envelope("2", {
            oneofKind: "runCreated",
            runCreated: { summary: created, nodes: [], topology },
          }),
          envelope("3", {
            oneofKind: "runStatusChanged",
            runStatusChanged: {
              runId: created.runId,
              status: "success",
              startedAt: 1,
              endedAt: 2,
              revision: "2",
            },
          }),
        ],
      },
    );

    expect(state.eventUlid).toBe(eventUlid(3));
    expect(state.runs[created.runId]).toMatchObject({ status: "success", revision: "2" });
  });
});

describe("useOperatorProjection", () => {
  it("backs off failed connections up to one second and reconnects", async () => {
    vi.useFakeTimers();
    const failure = new Error("connection refused");
    const loadBaseline = vi
      .fn<(signal?: AbortSignal) => Promise<StructuralBaseline>>()
      .mockRejectedValueOnce(failure)
      .mockRejectedValueOnce(failure)
      .mockRejectedValueOnce(failure)
      .mockRejectedValueOnce(failure)
      .mockResolvedValue(baseline);
    const api = createApi({ loadBaseline });
    vi.spyOn(console, "info").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { result, unmount } = renderHook(() => useOperatorProjection(api));
    const advance = async (milliseconds: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(milliseconds);
      });
    };

    await advance(0);
    expect(loadBaseline).toHaveBeenCalledTimes(1);
    await advance(249);
    expect(loadBaseline).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(loadBaseline).toHaveBeenCalledTimes(2);
    await advance(499);
    expect(loadBaseline).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(loadBaseline).toHaveBeenCalledTimes(3);
    await advance(999);
    expect(loadBaseline).toHaveBeenCalledTimes(3);
    await advance(1);
    expect(loadBaseline).toHaveBeenCalledTimes(4);
    await advance(999);
    expect(loadBaseline).toHaveBeenCalledTimes(4);
    await advance(1);
    expect(loadBaseline).toHaveBeenCalledTimes(5);
    expect(result.current.state.connection).toBe("live");

    unmount();
  });

  it("cancels a superseded selected snapshot and ignores its stale result", async () => {
    const first = deferred<RunSnapshotMsg>();
    const second = deferred<RunSnapshotMsg>();
    const signals: AbortSignal[] = [];
    const getLatestRunSnapshot = vi.fn(
      (runId: string, _operatorInstanceId: string, signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return runId === summary.runId ? first.promise : second.promise;
      },
    );
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs: [summary, secondSummary] }),
      getLatestRunSnapshot,
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));

    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("loading"));
    act(() => {
      void result.current.selectRun(secondSummary.runId);
    });

    expect(signals[0].aborted).toBe(true);
    act(() => second.resolve(snapshotFor(secondSummary)));
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));

    act(() => first.resolve(snapshotFor(summary)));
    await act(async () => {
      await first.promise;
    });
    expect(result.current.state.selectedRunId).toBe(secondSummary.runId);
    expect(result.current.state.selectedRun?.summary?.runId).toBe(secondSummary.runId);
  });

  it("aborts a selected snapshot on baseline replacement and ignores its stale result", async () => {
    const releaseReset = deferred<void>();
    const stale = deferred<RunSnapshotMsg>();
    const selectionSignals: AbortSignal[] = [];
    const replacement: StructuralBaseline = {
      catalog: CatalogSnapshotMsg.create({
        ...baseline.catalog,
        asOfEventUlid: eventUlid(8),
        revision: "2",
      }),
      asOfEventUlid: eventUlid(8),
      runs: [summary],
    };
    const loadBaseline = vi
      .fn<(signal?: AbortSignal) => Promise<StructuralBaseline>>()
      .mockResolvedValueOnce(baseline)
      .mockResolvedValue(replacement);
    let streamCount = 0;
    const streamUpdates = vi.fn(
      (_operatorInstanceId: string, _afterSequence: string, signal?: AbortSignal) => {
        streamCount += 1;
        if (streamCount > 1) return idleUpdates(signal);
        return (async function* () {
          await releaseReset.promise;
          yield OperatorUpdateEnvelope.create({
            operatorInstanceId: "operator-1",
            payload: {
              oneofKind: "resetRequired",
              resetRequired: {
                historyFloorEventUlid: eventUlid(2),
                latestEventUlid: eventUlid(8),
              },
            },
          });
        })();
      },
    );
    const api = createApi({
      loadBaseline,
      streamUpdates,
      getLatestRunSnapshot: (_runId, _operatorInstanceId, signal) => {
        if (signal) selectionSignals.push(signal);
        return stale.promise;
      },
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));

    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(selectionSignals).toHaveLength(1));
    act(() => releaseReset.resolve());
    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(8)));

    expect(selectionSignals[0].aborted).toBe(true);
    expect(result.current.state.selectedRunStatus).toBe("idle");
    act(() => stale.resolve(snapshotFor(summary)));
    await act(async () => {
      await stale.promise;
    });
    expect(result.current.state.selectedRunId).toBeUndefined();
    expect(result.current.state.selectedRun).toBeUndefined();
  });

  it("applies contiguous stream envelopes in frame batches of at most 256", async () => {
    const callbacks: FrameRequestCallback[] = [];
    let frameId = 0;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callbacks.push(callback);
      frameId += 1;
      return frameId;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    let produced = false;
    const streamUpdates = vi.fn(
      (_operatorInstanceId: string, _afterSequence: string, signal?: AbortSignal) =>
        (async function* () {
          for (let sequence = 2; sequence <= 601; sequence += 1) {
            yield envelope(String(sequence));
          }
          produced = true;
          yield* idleUpdates(signal);
        })(),
    );
    const api = createApi({ streamUpdates });
    const { result } = renderHook(() => useOperatorProjection(api));

    await waitFor(() => expect(produced).toBe(true));
    expect(callbacks).toHaveLength(1);
    act(() => callbacks.shift()?.(0));
    expect(result.current.state.eventUlid).toBe(eventUlid(257));
    expect(callbacks).toHaveLength(1);
    act(() => callbacks.shift()?.(1));
    expect(result.current.state.eventUlid).toBe(eventUlid(513));
    expect(callbacks).toHaveLength(1);
    act(() => callbacks.shift()?.(2));
    expect(result.current.state.eventUlid).toBe(eventUlid(601));
    expect(callbacks).toHaveLength(0);
  });

  it("aborts an overflowing pending queue and reconciles from a new baseline", async () => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    const cancelAnimationFrame = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation(() => undefined);
    const replacement: StructuralBaseline = {
      catalog: CatalogSnapshotMsg.create({
        ...baseline.catalog,
        asOfEventUlid: eventUlid(2000),
        revision: "2",
      }),
      asOfEventUlid: eventUlid(2000),
      runs: [summary],
    };
    const loadBaseline = vi
      .fn<(signal?: AbortSignal) => Promise<StructuralBaseline>>()
      .mockResolvedValueOnce(baseline)
      .mockResolvedValue(replacement);
    const streamSignals: AbortSignal[] = [];
    let streamCount = 0;
    const streamUpdates = vi.fn(
      (_operatorInstanceId: string, _afterSequence: string, signal?: AbortSignal) => {
        if (signal) streamSignals.push(signal);
        streamCount += 1;
        if (streamCount > 1) return idleUpdates(signal);
        return (async function* () {
          for (let sequence = 2; sequence <= 1026; sequence += 1) {
            yield envelope(String(sequence));
          }
        })();
      },
    );
    const api = createApi({ loadBaseline, streamUpdates });
    const { result } = renderHook(() => useOperatorProjection(api));

    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(2000)));
    expect(loadBaseline).toHaveBeenCalledTimes(2);
    expect(streamSignals[0].aborted).toBe(true);
    expect(cancelAnimationFrame).toHaveBeenCalled();
  });

  it.each([
    ["epoch change", envelope("2", undefined, "operator-2")],
    ["sequence gap", envelope("3")],
    [
      "reset notice",
      OperatorUpdateEnvelope.create({
        operatorInstanceId: "operator-1",
        payload: {
          oneofKind: "resetRequired",
          resetRequired: {
            historyFloorEventUlid: eventUlid(2),
            latestEventUlid: eventUlid(8),
          },
        },
      }),
    ],
    [
      "unknown run status",
      envelope("2", {
        oneofKind: "runStatusChanged",
        runStatusChanged: {
          runId: "run-missing",
          status: "failed",
          startedAt: 0,
          endedAt: 0,
          revision: "1",
        },
      }),
    ],
  ])("aborts and reconciles after a stream %s", async (_name, invalidEnvelope) => {
    const replacement: StructuralBaseline = {
      catalog: CatalogSnapshotMsg.create({
        ...baseline.catalog,
        asOfEventUlid: eventUlid(8),
        revision: "2",
      }),
      asOfEventUlid: eventUlid(8),
      runs: [summary],
    };
    const loadBaseline = vi
      .fn<(signal?: AbortSignal) => Promise<StructuralBaseline>>()
      .mockResolvedValueOnce(baseline)
      .mockResolvedValue(replacement);
    const streamSignals: AbortSignal[] = [];
    let streamCount = 0;
    const streamUpdates = vi.fn(
      (_operatorInstanceId: string, _afterSequence: string, signal?: AbortSignal) => {
        if (signal) streamSignals.push(signal);
        streamCount += 1;
        if (streamCount > 1) return idleUpdates(signal);
        return (async function* () {
          yield invalidEnvelope;
        })();
      },
    );
    const api = createApi({ loadBaseline, streamUpdates });
    const { result } = renderHook(() => useOperatorProjection(api));

    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(8)));
    expect(loadBaseline).toHaveBeenCalledTimes(2);
    expect(streamSignals[0].aborted).toBe(true);
  });

  it("retries instead of committing an N snapshot after N+1 projection state", async () => {
    const releaseUpdate = deferred<void>();
    const stale = deferred<RunSnapshotMsg>();
    const current = deferred<RunSnapshotMsg>();
    const getLatestRunSnapshot = vi
      .fn<ProjectionApi["getLatestRunSnapshot"]>()
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => current.promise);
    const api = createApi({
      getLatestRunSnapshot,
      streamUpdates: (_operatorInstanceId, _afterSequence, signal) =>
        (async function* () {
          await releaseUpdate.promise;
          yield envelope("2", {
            oneofKind: "nodeStatusChanged",
            nodeStatusChanged: {
              runId: summary.runId,
              nodeId: node.nodeId,
              status: "complete",
              revision: "2",
              startedAt: 1,
              endedAt: 2,
            },
          });
          yield* idleUpdates(signal);
        })(),
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));

    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(1));
    act(() => releaseUpdate.resolve());
    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(2)));
    act(() => stale.resolve(snapshotFor(summary)));

    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
    expect(result.current.state.selectedRunStatus).toBe("loading");
    expect(result.current.state.selectedRun).toBeUndefined();

    act(() =>
      current.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(2),
          nodes: [NodeSnapshotMsg.create({ ...node, status: "complete", revision: "2" })],
        }),
      ),
    );
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));
    expect(result.current.state.selectedRun?.nodes[0]).toMatchObject({
      status: "complete",
      revision: "2",
    });
  });

  it("refreshes exactly once after overflow and replaces the gap with a fresh snapshot", async () => {
    const startUpdates = deferred<void>();
    const repaired = deferred<RunSnapshotMsg>();
    const getLatestRunSnapshot = vi
      .fn<ProjectionApi["getLatestRunSnapshot"]>()
      .mockResolvedValueOnce(snapshotFor(summary))
      .mockImplementationOnce(() => repaired.promise);
    const api = createApi({
      getLatestRunSnapshot,
      streamUpdates: (_operatorInstanceId, _afterSequence, signal) =>
        (async function* () {
          await startUpdates.promise;
          for (let index = 1; index <= 257; index += 1) {
            yield envelope(String(index + 1), {
              oneofKind: "logAppended",
              logAppended: {
                runId: summary.runId,
                log: LogRecordDescriptorMsg.create({
                  sequence: String(index),
                  nodeId: node.nodeId,
                }),
              },
            });
          }
          yield* idleUpdates(signal);
        })(),
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));
    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));

    act(() => startUpdates.resolve());
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
    expect(result.current.state.liveLogs["run-1"]).toHaveLength(256);
    expect(result.current.state.liveLogRepairWatermarks["run-1"]).toBe("1");

    act(() =>
      repaired.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(258),
          logPageToken: "logs-through-257",
        }),
      ),
    );
    await waitFor(() =>
      expect(result.current.state.selectedRun?.logPageToken).toBe("logs-through-257"),
    );
    expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2);
    expect(result.current.state.liveLogs).toEqual({});
    expect(result.current.state.liveLogRepairWatermarks).toEqual({});
  });

  it("aborts an obsolete overflow refresh and suppresses its stale snapshot", async () => {
    const startUpdates = deferred<void>();
    const appendAgain = deferred<void>();
    const obsolete = deferred<RunSnapshotMsg>();
    const replacement = deferred<RunSnapshotMsg>();
    const repairSignals: AbortSignal[] = [];
    let request = 0;
    const getLatestRunSnapshot = vi.fn<ProjectionApi["getLatestRunSnapshot"]>(
      (_runId, _operatorInstanceId, signal) => {
        request += 1;
        if (request === 1) return Promise.resolve(snapshotFor(summary));
        if (signal) repairSignals.push(signal);
        return request === 2 ? obsolete.promise : replacement.promise;
      },
    );
    const api = createApi({
      getLatestRunSnapshot,
      streamUpdates: (_operatorInstanceId, _afterSequence, signal) =>
        (async function* () {
          await startUpdates.promise;
          for (let index = 1; index <= 257; index += 1) {
            yield envelope(String(index + 1), {
              oneofKind: "logAppended",
              logAppended: {
                runId: summary.runId,
                log: LogRecordDescriptorMsg.create({
                  sequence: String(index),
                  nodeId: node.nodeId,
                }),
              },
            });
          }
          await appendAgain.promise;
          yield envelope("259", {
            oneofKind: "logAppended",
            logAppended: {
              runId: summary.runId,
              log: LogRecordDescriptorMsg.create({ sequence: "258", nodeId: node.nodeId }),
            },
          });
          yield* idleUpdates(signal);
        })(),
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));
    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));

    act(() => startUpdates.resolve());
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
    act(() => appendAgain.resolve());
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(3));
    expect(repairSignals[0].aborted).toBe(true);

    act(() =>
      replacement.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(259),
          logPageToken: "replacement-token",
        }),
      ),
    );
    await waitFor(() =>
      expect(result.current.state.selectedRun?.logPageToken).toBe("replacement-token"),
    );
    act(() =>
      obsolete.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(258),
          logPageToken: "obsolete-token",
        }),
      ),
    );
    await act(async () => {
      await obsolete.promise;
    });

    expect(result.current.state.selectedRun?.logPageToken).toBe("replacement-token");
    expect(result.current.state.liveLogs).toEqual({});
    expect(result.current.state.liveLogRepairWatermarks).toEqual({});
  });

  it("aborts stream and selected snapshot work on cleanup", async () => {
    let streamSignal: AbortSignal | undefined;
    let selectionSignal: AbortSignal | undefined;
    const selected = deferred<RunSnapshotMsg>();
    const api = createApi({
      streamUpdates: (_operatorInstanceId, _afterSequence, signal) => {
        streamSignal = signal;
        return idleUpdates(signal);
      },
      getLatestRunSnapshot: (_runId, _operatorInstanceId, signal) => {
        selectionSignal = signal;
        return selected.promise;
      },
    });
    const { result, unmount } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));
    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("loading"));

    unmount();

    expect(streamSignal?.aborted).toBe(true);
    expect(selectionSignal?.aborted).toBe(true);
  });
});
