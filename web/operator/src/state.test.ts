import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import {
  CatalogSnapshotMsg,
  LogRecordDescriptorMsg,
  NodeSnapshotMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
} from "./model";
import { useOperatorProjection } from "./state";
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

  it("retains authored skip detail when a selected running node completes live", async () => {
    const release = Promise.withResolvers<void>();
    const skip = { reason: "No eligible records", metadataJson: '{"count":0}' };
    const api = createApi({
      streamUpdates: async function* (_instance, _cursor, signal) {
        await release.promise;
        yield envelope(2, {
          oneofKind: "nodeStatusChanged",
          nodeStatusChanged: {
            runId: summary.runId,
            nodeId: node.nodeId,
            status: "skipped",
            revision: "2",
            startedAt: 1,
            endedAt: 2,
            skip,
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
    await waitFor(() => expect(result.current.state.selectedRunStatus).toBe("ready"));
    act(() => release.resolve());
    await waitFor(() =>
      expect(result.current.state.selectedRun?.nodes[0].status).toBe("skipped"),
    );
    expect(result.current.state.selectedRun?.nodes[0].skip).toEqual(skip);
    expect(result.current.state.selectedRun?.nodes[0].error).toBeUndefined();
  });

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

  it("repairs an overflowing live tail without accepting an obsolete repair snapshot", async () => {
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
          yield envelope(sequence + 1, {
            oneofKind: "logAppended",
            logAppended: {
              runId: summary.runId,
              log: LogRecordDescriptorMsg.create({
                sequence: String(sequence),
                nodeId: node.nodeId,
              }),
            },
          });
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
    expect(result.current.state.liveLogs[summary.runId]).toHaveLength(256);
    act(() => appendAgain.resolve());
    await waitFor(() => expect(getLatestRunSnapshot).toHaveBeenCalledTimes(3));
    expect(repairSignal?.aborted).toBe(true);
    act(() =>
      repaired.resolve(
        RunSnapshotMsg.create({
          ...snapshotFor(summary),
          asOfEventUlid: eventUlid(259),
          logPageToken: "logs-through-258",
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
        }),
      );
      await obsolete.promise;
    });
    expect(result.current.state.selectedRun?.logPageToken).toBe("logs-through-258");
    expect(result.current.state.liveLogs).toEqual({});
  });
});
