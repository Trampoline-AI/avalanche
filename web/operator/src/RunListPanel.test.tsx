import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 32,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        size: 32,
        start: index * 32,
      })),
  }),
}));

import { RunSummaryMsg } from "./generated/operator";
import { RunListPanel } from "./RunListPanel";

function run(
  runId: string,
  workflowId: string,
  createdSequence: string,
  status: string,
  startedAt: number,
  endedAt: number,
) {
  return RunSummaryMsg.create({
    runId,
    workflowId,
    workflowDisplayName: workflowId,
    createdSequence,
    status,
    startedAt,
    endedAt,
  });
}

describe("RunListPanel", () => {
  it("shows compact newest-first rows with status and duration", () => {
    render(
      <RunListPanel
        workflowId="flows.py::orders"
        runs={{
          older: run("run-older", "flows.py::orders", "2", "success", 10, 12.25),
          newer: run("run-newer", "flows.py::orders", "9007199254740993", "failed", 20, 21),
          unrelated: run("run-other", "flows.py::inventory", "3", "success", 1, 2),
        }}
        selectedRunId="run-older"
        onSelectRun={vi.fn()}
      />,
    );

    const rows = screen.getAllByRole("button");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAccessibleName("run-newer, failed, 1.0s");
    expect(rows[1]).toHaveAccessibleName("run-older, success, 2.3s");
    expect(rows[1]).toHaveClass("active");
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("selects a run and represents incomplete duration explicitly", () => {
    const onSelectRun = vi.fn();
    render(
      <RunListPanel
        workflowId="flows.py::orders"
        runs={{ active: run("run-active", "flows.py::orders", "4", "running", 10, 0) }}
        onSelectRun={onSelectRun}
      />,
    );

    const row = screen.getByRole("button", { name: "run-active, running, —" });
    fireEvent.click(row);
    expect(onSelectRun).toHaveBeenCalledWith("run-active");
  });
});
