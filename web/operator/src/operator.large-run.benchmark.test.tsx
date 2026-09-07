import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunListPanel } from "./RunListPanel";
import { RunSummaryMsg } from "./model";
import { useOperatorProjection } from "./state";
import {
  baseline,
  createApi,
  envelope,
  eventUlid,
  idleUpdates,
  summary,
  workflow,
} from "./test/fixtures";

afterEach(() => vi.restoreAllMocks());

describe("large-run resource bounds", () => {
  it("uses the real virtualizer to keep 10,000 runs navigable without mounting 10,000 rows", async () => {
    const runs = Object.fromEntries(
      Array.from({ length: 10_000 }, (_, index) => {
        const runId = `run-${index.toString().padStart(5, "0")}`;
        return [
          runId,
          RunSummaryMsg.create({
            ...summary,
            runId,
            createdSequence: String(9007199254740993n + BigInt(index)),
          }),
        ];
      }),
    );
    const selected = vi.fn();
    const view = render(
      <RunListPanel workflowId={workflow.workflowId} runs={runs} onSelectRun={selected} />,
    );
    const region = screen.getByRole("region", { name: "Workflow runs" });
    const newest = await within(region).findByRole("button", { name: /run-09999,/ });
    expect(within(region).getAllByRole("button").length).toBeLessThan(100);
    expect(screen.queryByRole("button", { name: /run-00000,/ })).not.toBeInTheDocument();
    fireEvent.click(newest);
    expect(selected).toHaveBeenLastCalledWith("run-09999");

    // Scroll to real off-screen data: a stub that simply slices the first rows fails here.
    const scroll = view.container.querySelector<HTMLElement>(".run-list-scroll")!;
    scroll.scrollTop = 10_000 * 32 - 192;
    fireEvent.scroll(scroll);
    const oldest = await within(region).findByRole("button", { name: /run-00000,/ });
    expect(within(region).getAllByRole("button").length).toBeLessThan(100);
    fireEvent.click(oldest);
    expect(selected).toHaveBeenLastCalledWith("run-00000");
  });

  it("yields between live-update batches without losing order or the final status", async () => {
    const frames = new Map<number, FrameRequestCallback>();
    let frameId = 0;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.set(++frameId, callback);
      return frameId;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
      frames.delete(id);
    });
    let produced = false;
    const api = createApi({
      streamUpdates: async function* (_instance, _cursor, signal) {
        for (let sequence = 2; sequence <= 1001; sequence += 1) {
          yield envelope(sequence, {
            oneofKind: "runStatusChanged",
            runStatusChanged: {
              runId: summary.runId,
              status: sequence === 1001 ? "success" : "running",
              revision: String(sequence),
              startedAt: 1,
              endedAt: sequence === 1001 ? 2 : 0,
            },
          });
        }
        produced = true;
        yield* idleUpdates(signal);
      },
    });
    const { result } = renderHook(() => useOperatorProjection(api));
    await waitFor(() => expect(produced).toBe(true));
    expect(result.current.state.eventUlid).toBe(baseline.asOfEventUlid);
    let previous = 1;
    let commits = 0;
    while (frames.size) {
      const [id, callback] = frames.entries().next().value!;
      frames.delete(id);
      act(() => callback(id * 16));
      const applied = Number.parseInt(result.current.state.eventUlid, 16);
      expect(applied).toBeGreaterThan(previous);
      expect(applied - previous).toBeLessThanOrEqual(256);
      previous = applied;
      commits += 1;
    }
    expect(commits).toBeGreaterThan(1);
    expect(result.current.state.eventUlid).toBe(eventUlid(1001));
    expect(result.current.state.runs[summary.runId].status).toBe("success");
  });
});
