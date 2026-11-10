import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OperatorApi } from "./api";
import type { MachineInstance } from "./machine";
import { useMachineInstance } from "./MachineWorkspace";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

/** A host whose instance read takes `latency` ms and returns the revision it read. */
function slowHost(latency: number) {
  const stats = { started: 0, completed: 0, aborted: 0, inFlight: 0, maxInFlight: 0 };
  let revision = 0;
  const api = {
    getMachineInstance: (_runId: string, signal?: AbortSignal) =>
      new Promise<MachineInstance>((resolve, reject) => {
        stats.started += 1;
        stats.inFlight += 1;
        stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
        const seen = revision;
        let done = false;
        const timer = setTimeout(() => {
          done = true;
          stats.inFlight -= 1;
          stats.completed += 1;
          resolve({ revision: seen } as unknown as MachineInstance);
        }, latency);
        signal?.addEventListener("abort", () => {
          if (done) return;
          done = true;
          clearTimeout(timer);
          stats.inFlight -= 1;
          stats.aborted += 1;
          reject(new DOMException("aborted", "AbortError"));
        });
      }),
  } as unknown as OperatorApi;
  return {
    api,
    stats,
    bump: () => (revision += 1),
    get revision() {
      return revision;
    },
  };
}

describe("machine reads under a revision burst", () => {
  it.each([300, 50])(
    "complete reads with %i ms latency and end on the last revision",
    async (latency) => {
      const host = slowHost(latency);
      const { result, rerender } = renderHook(
        ({ refreshKey }) => useMachineInstance(host.api, "run-1", { live: true, refreshKey }),
        { initialProps: { refreshKey: "0" } },
      );
      // A revision every 100 ms for 5 s.
      for (let tick = 0; tick < 50; tick += 1) {
        await act(() => vi.advanceTimersByTimeAsync(100));
        host.bump();
        rerender({ refreshKey: String(host.revision) });
      }
      const during = { ...host.stats };
      // Reads finish during the burst, one at a time, and none are thrown away.
      expect(during.completed).toBeGreaterThanOrEqual(latency === 300 ? 7 : 12);
      expect(during.aborted).toBe(0);
      expect(during.maxInFlight).toBe(1);
      // Coalesced: no more than one read per (latency + spacing).
      expect(during.started).toBeLessThanOrEqual(Math.ceil(5000 / (latency + 250)) + 1);
      // The follow-up read (after the read in flight, then the spacing) shows the last revision.
      await act(() => vi.advanceTimersByTimeAsync(2 * latency + 250));
      expect((result.current.value as unknown as { revision: number }).revision).toBe(
        host.revision,
      );
      expect(host.stats.aborted).toBe(0);
    },
  );

  it("aborts the read in flight when the run changes", async () => {
    const host = slowHost(300);
    const { rerender } = renderHook(({ runId }) => useMachineInstance(host.api, runId, {}), {
      initialProps: { runId: "run-1" },
    });
    await act(() => vi.advanceTimersByTimeAsync(100));
    rerender({ runId: "run-2" });
    expect(host.stats.aborted).toBe(1);
    expect(host.stats.started).toBe(2);
  });

  it("does not poll a settled run", async () => {
    const host = slowHost(50);
    renderHook(() => useMachineInstance(host.api, "run-1", { live: false, refreshKey: "1" }));
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(host.stats.started).toBe(1);
  });
});
