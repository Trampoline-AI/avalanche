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
} from "./generated/operator";
import { emptyProjection, projectionReducer, useOperatorProjection } from "./state";

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
    asOfSequence: "1",
    revision: "1",
    workflows: [workflow],
  }),
  asOfSequence: "1",
  runs: [summary],
};

function snapshotFor(run: RunSummaryMsg): RunSnapshotMsg {
  return RunSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfSequence: "1",
    summary: run,
    nodes: [node],
    topology,
  });
}

function envelope(
  sequence: string,
  change: OperatorUpdate["change"] = { oneofKind: undefined },
  operatorInstanceId = "operator-1",
): OperatorUpdateEnvelope {
  return OperatorUpdateEnvelope.create({
    operatorInstanceId,
    payload: { oneofKind: "update", update: { sequence, change } },
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
});

describe("projectionReducer", () => {
  it("installs summary-only baselines and clears selected ephemeral state", () => {
    const previous = {
      ...selectedState(),
      liveLogs: { "run-1:fetch": [LogRecordDescriptorMsg.create({ sequence: "1" })] },
      liveEvents: {
        "run-1:fetch": [AgentEventDescriptorMsg.create({ eventSequence: "1" })],
      },
      liveLogRepairWatermarks: { "run-1:fetch": "1" },
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

  it("orders and deduplicates live descriptor tails by run and node", () => {
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
            log: LogRecordDescriptorMsg.create({ sequence: "2", nodeId: "fetch" }),
          },
        }),
        envelope("5", {
          oneofKind: "logAppended",
          logAppended: {
            runId: summary.runId,
            log: LogRecordDescriptorMsg.create({ sequence: "2", nodeId: "fetch" }),
          },
        }),
      ],
    });

    expect(state.liveLogs["run-1:fetch"].map((entry) => entry.sequence)).toEqual([
      "1",
      "2",
      "3",
    ]);
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

    expect(state.liveLogs["run-1:fetch"]).toHaveLength(256);
    expect(state.liveLogs["run-1:fetch"][0].sequence).toBe("5");
    expect(state.liveLogRepairWatermarks["run-1:fetch"]).toBe("4");
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
      asOfSequence: "5",
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
        "run-1:fetch": [LogRecordDescriptorMsg.create({ sequence: "2" })],
        "run-2:fetch": [LogRecordDescriptorMsg.create({ sequence: "9" })],
      },
      liveEvents: {
        "run-1:fetch": [AgentEventDescriptorMsg.create({ eventSequence: "2" })],
      },
      liveLogRepairWatermarks: { "run-1:fetch": "2", "run-2:fetch": "9" },
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
    expect(state.liveLogs["run-1:fetch"]).toBeUndefined();
    expect(state.liveEvents["run-1:fetch"]).toBeUndefined();
    expect(state.liveLogRepairWatermarks).toEqual({ "run-2:fetch": "9" });
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
        asOfSequence: "2",
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
        resetRequired: { historyFloor: "2", latestSequence: "8" },
      },
    });

    expect(() =>
      projectionReducer(state, { type: "envelopes", envelopes: [envelope("2", undefined, "other")] }),
    ).toThrow("epoch changed");
    expect(() =>
      projectionReducer(state, { type: "envelopes", envelopes: [envelope("3")] }),
    ).toThrow("update gap");
    expect(() =>
      projectionReducer(state, { type: "envelopes", envelopes: [reset] }),
    ).toThrow("structural reset");
  });
});

describe("useOperatorProjection", () => {
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
    expect(result.current.state.sequence).toBe("257");
    expect(callbacks).toHaveLength(1);
    act(() => callbacks.shift()?.(1));
    expect(result.current.state.sequence).toBe("513");
    expect(callbacks).toHaveLength(1);
    act(() => callbacks.shift()?.(2));
    expect(result.current.state.sequence).toBe("601");
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
        asOfSequence: "2000",
        revision: "2",
      }),
      asOfSequence: "2000",
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

    await waitFor(() => expect(result.current.state.sequence).toBe("2000"));
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
          resetRequired: { historyFloor: "2", latestSequence: "8" },
        },
      }),
    ],
  ])("aborts and reconciles after a stream %s", async (_name, invalidEnvelope) => {
    const replacement: StructuralBaseline = {
      catalog: CatalogSnapshotMsg.create({
        ...baseline.catalog,
        asOfSequence: "8",
        revision: "2",
      }),
      asOfSequence: "8",
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

    await waitFor(() => expect(result.current.state.sequence).toBe("8"));
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
    await waitFor(() => expect(result.current.state.sequence).toBe("2"));
    act(() => stale.resolve(snapshotFor(summary)));

    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
    expect(result.current.state.selectedRunStatus).toBe("loading");
    expect(result.current.state.selectedRun).toBeUndefined();

    act(() =>
      current.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfSequence: "2",
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
    expect(result.current.state.liveLogs["run-1:fetch"]).toHaveLength(256);
    expect(result.current.state.liveLogRepairWatermarks["run-1:fetch"]).toBe("1");

    act(() =>
      repaired.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfSequence: "258",
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
          asOfSequence: "259",
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
          asOfSequence: "258",
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
