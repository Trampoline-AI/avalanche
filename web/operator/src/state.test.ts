import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  ClassifierEventDescriptorMsg,
  LogRecordDescriptorMsg,
  NodeSnapshotMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  WorkflowTopologyMsg,
} from "./model";
import { emptyProjection, projectionReducer, useOperatorProjection } from "./state";
import {
  baseline,
  createApi,
  envelope,
  eventUlid,
  idleUpdates,
  node,
  secondSummary,
  snapshotFor,
  summary,
} from "./test/fixtures";

afterEach(() => vi.useRealTimers());

describe("live operator projection", () => {
  it("reconnects after an unavailable operator without an immediate retry loop", async () => {
    vi.useFakeTimers();
    const loadBaseline = vi
      .fn()
      .mockRejectedValueOnce(new Error("Operator unavailable"))
      .mockResolvedValue(baseline);
    const api = createApi({ loadBaseline });
    const { result, unmount } = renderHook(() => useOperatorProjection(api));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.state.connection).toBe("reconnecting");
    expect(loadBaseline).toHaveBeenCalledOnce();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.state.connection).toBe("live");
    expect(result.current.state.runs[summary.runId].status).toBe("running");
    unmount();
  });

  it("keeps the usable catalog until a complete reload arrives and coalesces repeated notices", async () => {
    const catalog = Promise.withResolvers<CatalogSnapshotMsg>();
    const getCatalog = vi.fn(() => catalog.promise);
    const api = createApi({
      getCatalog,
      streamUpdates: async function* (_instance, _cursor, signal) {
        for (const sequence of [2, 3]) {
          yield envelope(sequence, {
            oneofKind: "catalogReloadRequired",
            catalogReloadRequired: { deploymentId: "deployment-a" },
          });
        }
        yield* idleUpdates(signal);
      },
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(getCatalog).toHaveBeenCalledOnce());
    expect(result.current.state.catalog?.workflows).toEqual(baseline.catalog.workflows);
    act(() =>
      catalog.resolve(
        CatalogSnapshotMsg.create({
          ...baseline.catalog,
          asOfEventUlid: eventUlid(2),
          revision: "2",
          workflows: [],
        }),
      ),
    );
    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(3)));
    expect(result.current.state.catalog?.workflows).toEqual([]);
    expect(getCatalog).toHaveBeenCalledOnce();
  });

  it("cancels superseded selections and never lets a late snapshot replace the visible run", async () => {
    const first = Promise.withResolvers<RunSnapshotMsg>();
    const second = Promise.withResolvers<RunSnapshotMsg>();
    const signals: AbortSignal[] = [];
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs: [summary, secondSummary] }),
      getLatestRunSnapshot: (runId, _instance, signal) => {
        signals.push(signal!);
        return runId === summary.runId ? first.promise : second.promise;
      },
    });
    const { result, unmount } = renderHook(() => useOperatorProjection(api));
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
    await act(async () => {
      first.resolve(snapshotFor(summary));
      await first.promise;
    });
    expect(result.current.state.selectedRun?.summary?.runId).toBe(secondSummary.runId);

    act(() => {
      void result.current.selectRun(summary.runId);
    });
    unmount();
    expect(signals.at(-1)?.aborted).toBe(true);
  });

  it.each([
    ["operator replacement", envelope(2, undefined, "operator-2")],
    ["backward cursor", envelope(0)],
    [
      "server reset",
      OperatorUpdateEnvelope.create({
        operatorInstanceId: "operator-1",
        payload: {
          oneofKind: "resetRequired",
          resetRequired: { historyFloorEventUlid: eventUlid(2), latestEventUlid: eventUlid(8) },
        },
      }),
    ],
  ])(
    "reconciles a %s and discards in-flight data from the previous baseline",
    async (_name, invalid) => {
      const release = Promise.withResolvers<void>();
      const stale = Promise.withResolvers<RunSnapshotMsg>();
      const replacement = {
        ...baseline,
        catalog: CatalogSnapshotMsg.create({
          ...baseline.catalog,
          asOfEventUlid: eventUlid(8),
        }),
        asOfEventUlid: eventUlid(8),
        runs: [{ ...summary, status: "success" }],
      };
      let streamSignal: AbortSignal | undefined;
      let snapshotSignal: AbortSignal | undefined;
      let connections = 0;
      const api = createApi({
        loadBaseline: vi.fn().mockResolvedValueOnce(baseline).mockResolvedValue(replacement),
        getLatestRunSnapshot: (_run, _instance, signal) => {
          snapshotSignal = signal;
          return stale.promise;
        },
        streamUpdates: async function* (_instance, _cursor, signal) {
          if (connections++ === 0) {
            streamSignal = signal;
            await release.promise;
            yield invalid;
          }
          yield* idleUpdates(signal);
        },
      });
      const { result } = renderHook(() => useOperatorProjection(api));
      await waitFor(() => expect(result.current.state.connection).toBe("live"));
      act(() => {
        void result.current.selectRun(summary.runId);
      });
      await waitFor(() => expect(snapshotSignal).toBeDefined());
      act(() => release.resolve());
      await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(8)));
      expect(streamSignal?.aborted).toBe(true);
      expect(snapshotSignal?.aborted).toBe(true);
      await act(async () => {
        stale.resolve(snapshotFor(summary));
        await stale.promise;
      });
      expect(result.current.state.runs[summary.runId].status).toBe("success");
      expect(result.current.state.selectedRun).toBeUndefined();
      expect(result.current.state.connection).toBe("live");
    },
  );

  it("retries an overtaken snapshot rather than rolling live node status backward", async () => {
    const release = Promise.withResolvers<void>();
    const stale = Promise.withResolvers<RunSnapshotMsg>();
    const current = Promise.withResolvers<RunSnapshotMsg>();
    const getLatestRunSnapshot = vi
      .fn<OperatorApi["getLatestRunSnapshot"]>()
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => current.promise);
    const api = createApi({
      getLatestRunSnapshot,
      streamUpdates: async function* (_instance, _cursor, signal) {
        await release.promise;
        yield envelope(2, {
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: summary.runId,
            nodeId: node.nodeId,
            status: "success",
            revision: "2",
            startedAt: 1,
            endedAt: 2,
          },
        });
        yield* idleUpdates(signal);
      },
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));
    act(() => {
      void result.current.selectRun(summary.runId);
    });
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledOnce());
    act(() => release.resolve());
    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(2)));
    act(() => stale.resolve(snapshotFor(summary)));
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
    expect(result.current.state.selectedRun).toBeUndefined();
    act(() =>
      current.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(2),
          nodes: [NodeSnapshotMsg.create({ ...node, status: "success", revision: "2" })],
        }),
      ),
    );
    await waitFor(() =>
      expect(result.current.state.selectedRun?.nodes[0].status).toBe("success"),
    );
  });

  it("refreshes a requesting snapshot with the prepared run topology and node history", async () => {
    const release = Promise.withResolvers<void>();
    const prepared = Promise.withResolvers<RunSnapshotMsg>();
    const requestingSummary = { ...summary, status: "requesting" };
    const declaration = JSON.stringify({
      questions: {
        accepted: { type: "noul", instructions: "Prepared definition", criteria: null },
      },
      runtime: { model: "jev-latest", timeout: 10 },
    });
    const preparedTopology = WorkflowTopologyMsg.create({
      nodeIds: [node.nodeId],
      nodeTypes: { [node.nodeId]: "step" },
      classifierMetadataJson: { [node.nodeId]: declaration },
    });
    const getLatestRunSnapshot = vi
      .fn<OperatorApi["getLatestRunSnapshot"]>()
      .mockResolvedValueOnce(
        RunSnapshotMsg.create({
          ...snapshotFor(requestingSummary),
          nodes: [],
          topology: WorkflowTopologyMsg.create(),
        }),
      )
      .mockImplementationOnce(() => prepared.promise);
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs: [requestingSummary] }),
      getLatestRunSnapshot,
      streamUpdates: async function* (_instance, _cursor, signal) {
        await release.promise;
        yield envelope(2, {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId: summary.runId,
            status: "pending",
            startedAt: 0,
            endedAt: 0,
            revision: "2",
          },
        });
        yield envelope(3, {
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: summary.runId,
            nodeId: node.nodeId,
            status: "running",
            startedAt: 1,
            endedAt: 0,
            revision: "3",
          },
        });
        yield* idleUpdates(signal);
      },
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(result.current.state.connection).toBe("live"));
    await act(async () => {
      await result.current.selectRun(summary.runId);
    });
    expect(result.current.state.selectedRun?.nodes).toEqual([]);
    act(() => release.resolve());
    await waitFor(() => expect(result.current.state.eventUlid).toBe(eventUlid(3)));
    act(() =>
      prepared.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor({ ...summary, status: "pending", revision: "2" }),
          asOfEventUlid: eventUlid(3),
          topology: preparedTopology,
          nodes: [
            NodeSnapshotMsg.create({
              ...node,
              eventPageToken: "prepared-history",
              revision: "3",
            }),
          ],
        }),
      ),
    );
    await waitFor(() =>
      expect(result.current.state.selectedRun?.nodes[0]?.nodeId).toBe(node.nodeId),
    );
    expect(result.current.state.selectedRun?.topology).toEqual(preparedTopology);
    expect(result.current.state.selectedRun?.nodes[0].eventPageToken).toBe("prepared-history");
    expect(result.current.state.selectedRunStructureRepairEventUlid).toBeUndefined();
  });

  it.each(["logs", "classifier"])(
    "repairs an overflowing %s tail without accepting an obsolete repair snapshot",
    async (kind) => {
      const release = Promise.withResolvers<void>();
      const appendAgain = Promise.withResolvers<void>();
      const obsolete = Promise.withResolvers<RunSnapshotMsg>();
      const repaired = Promise.withResolvers<RunSnapshotMsg>();
      let repairSignal: AbortSignal | undefined;
      const getLatestRunSnapshot = vi
        .fn<OperatorApi["getLatestRunSnapshot"]>()
        .mockResolvedValueOnce(snapshotFor(summary))
        .mockImplementationOnce((_run, _instance, signal) => {
          repairSignal = signal;
          return obsolete.promise;
        })
        .mockImplementationOnce(() => repaired.promise);
      const api = createApi({
        getLatestRunSnapshot,
        streamUpdates: async function* (_instance, _cursor, signal) {
          await release.promise;
          for (let sequence = 1; sequence <= 258; sequence += 1) {
            if (sequence === 258) await appendAgain.promise;
            yield envelope(
              sequence + 1,
              kind === "classifier"
                ? {
                    oneofKind: "classifierEventAppended",
                    classifierEventAppended: {
                      runId: summary.runId,
                      nodeId: node.nodeId,
                      event: ClassifierEventDescriptorMsg.create({
                        eventSequence: String(sequence),
                        invocationId: `call-${sequence}`,
                        eventKind: "success",
                      }),
                    },
                  }
                : {
                    oneofKind: "logAppended",
                    logAppended: {
                      runId: summary.runId,
                      log: LogRecordDescriptorMsg.create({
                        sequence: String(sequence),
                        nodeId: node.nodeId,
                      }),
                    },
                  },
            );
          }
          yield* idleUpdates(signal);
        },
      });
      const { result } = renderHook(() => useOperatorProjection(api));
      await waitFor(() => expect(result.current.state.connection).toBe("live"));
      act(() => {
        void result.current.selectRun(summary.runId);
      });
      await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));
      act(() => release.resolve());
      await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(2));
      expect(
        kind === "classifier"
          ? result.current.state.liveClassifierEvents[`${summary.runId}:${node.nodeId}`]
          : result.current.state.liveLogs[summary.runId],
      ).toHaveLength(256);
      act(() => appendAgain.resolve());
      await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(3));
      expect(repairSignal?.aborted).toBe(true);
      act(() =>
        repaired.resolve(
          RunSnapshotMsg.create({
            ...snapshotFor(summary),
            asOfEventUlid: eventUlid(259),
            logPageToken: "logs-through-258",
            nodes: [
              NodeSnapshotMsg.create({ ...node, eventPageToken: "classifier-through-258" }),
            ],
          }),
        ),
      );
      await waitFor(() =>
        expect(result.current.state.selectedRun?.logPageToken).toBe("logs-through-258"),
      );
      await act(async () => {
        obsolete.resolve(
          RunSnapshotMsg.create({
            ...snapshotFor(summary),
            asOfEventUlid: eventUlid(258),
            logPageToken: "obsolete",
            nodes: [NodeSnapshotMsg.create({ ...node, eventPageToken: "obsolete" })],
          }),
        );
        await obsolete.promise;
      });
      expect(result.current.state.selectedRun?.logPageToken).toBe("logs-through-258");
      expect(result.current.state.selectedRun?.nodes[0].eventPageToken).toBe(
        "classifier-through-258",
      );
      expect(result.current.state.liveLogs).toEqual({});
      expect(result.current.state.liveClassifierEvents).toEqual({});
      expect(result.current.state.liveClassifierEventRepairWatermarks).toEqual({});
    },
  );

  it("keeps classifier descriptors isolated from other runs, agents, and covered snapshot history", () => {
    let state = projectionReducer(emptyProjection, {
      type: "baseline",
      baseline: { ...baseline, runs: [summary, secondSummary] },
    });
    state = projectionReducer(state, { type: "selectionLoading", runId: summary.runId });
    state = projectionReducer(state, {
      type: "selectionReady",
      runId: summary.runId,
      snapshot: RunSnapshotMsg.create({ ...snapshotFor(summary), asOfEventUlid: eventUlid(4) }),
    });
    const event = ClassifierEventDescriptorMsg.create({
      eventSequence: "1",
      invocationId: "call-1",
      bodyToken: "classifier-call-1",
      eventKind: "success",
    });
    const append = (sequence: number, runId = summary.runId) =>
      envelope(sequence, {
        oneofKind: "classifierEventAppended",
        classifierEventAppended: { runId, nodeId: node.nodeId, event },
      });
    state = projectionReducer(state, { type: "envelopes", envelopes: [append(2)] });
    expect(state.liveClassifierEvents).toEqual({});
    state = projectionReducer(state, {
      type: "envelopes",
      envelopes: [
        append(5),
        append(6, secondSummary.runId),
        append(7),
        envelope(8, {
          oneofKind: "agentEventAppended",
          agentEventAppended: {
            runId: summary.runId,
            nodeId: node.nodeId,
            event: AgentEventDescriptorMsg.create({
              eventSequence: "1",
              invocationId: "agent-call",
              bodyToken: "agent-call-1",
            }),
          },
        }),
      ],
    });
    const key = `${summary.runId}:${node.nodeId}`;
    expect(state.liveClassifierEvents).toEqual({ [key]: [event] });
    expect(state.liveEvents[key][0].bodyToken).toBe("agent-call-1");
    expect(() =>
      projectionReducer(state, {
        type: "envelopes",
        envelopes: [append(9, "unknown-run")],
      }),
    ).toThrow(/unknown run/i);
    expect(() =>
      projectionReducer(state, {
        type: "envelopes",
        envelopes: [append(6)],
      }),
    ).toThrow(/backwards/i);
    expect(() =>
      projectionReducer(state, {
        type: "envelopes",
        envelopes: [{ ...append(9), operatorInstanceId: "replaced-operator" }],
      }),
    ).toThrow(/epoch changed/i);
    const otherSnapshot = RunSnapshotMsg.create({
      ...snapshotFor(secondSummary),
      asOfEventUlid: eventUlid(9),
    });
    expect(
      projectionReducer(state, {
        type: "selectionReady",
        runId: summary.runId,
        snapshot: otherSnapshot,
      }),
    ).toBe(state);
  });

  it("bounds classifier history by exact sequence and clears repair state on every rebaseline", () => {
    let state = projectionReducer(emptyProjection, { type: "baseline", baseline });
    state = projectionReducer(state, { type: "selectionLoading", runId: summary.runId });
    const firstSequence = 9007199254740993n;
    state = projectionReducer(state, {
      type: "envelopes",
      envelopes: Array.from({ length: 257 }, (_, index) =>
        envelope(index + 2, {
          oneofKind: "classifierEventAppended",
          classifierEventAppended: {
            runId: summary.runId,
            nodeId: node.nodeId,
            event: ClassifierEventDescriptorMsg.create({
              eventSequence: String(firstSequence + BigInt(256 - index)),
              invocationId: `call-${index}`,
            }),
          },
        }),
      ),
    });
    const key = `${summary.runId}:${node.nodeId}`;
    expect(state.liveClassifierEvents[key].map((event) => event.eventSequence)).toEqual(
      Array.from({ length: 256 }, (_, index) => String(firstSequence + BigInt(index + 1))),
    );
    expect(state.liveClassifierEventRepairWatermarks).toEqual({ [key]: String(firstSequence) });
    const repaired = projectionReducer(state, {
      type: "selectionReady",
      runId: summary.runId,
      snapshot: RunSnapshotMsg.create({
        ...snapshotFor(summary),
        asOfEventUlid: eventUlid(258),
        nodes: [
          NodeSnapshotMsg.create({ ...node, eventPageToken: "repaired-classifier-events" }),
        ],
      }),
    });
    expect(repaired.selectedRun?.nodes[0].eventPageToken).toBe("repaired-classifier-events");
    const switched = projectionReducer(state, {
      type: "selectionLoading",
      runId: secondSummary.runId,
    });
    const cleared = projectionReducer(state, { type: "selectionCleared" });
    const reconnected = projectionReducer(state, { type: "baseline", baseline });
    for (const reset of [repaired, switched, cleared, reconnected]) {
      expect(reset.liveClassifierEvents).toEqual({});
      expect(reset.liveClassifierEventRepairWatermarks).toEqual({});
    }
  });
});
