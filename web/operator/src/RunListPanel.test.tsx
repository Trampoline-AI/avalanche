import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";

import { RunListPanel } from "./RunListPanel";
import { RunSummaryMsg } from "./model";
import { summary, workflow } from "./test/fixtures";

async function scrollTimelineTo(element: HTMLElement, top: number) {
  vi.useFakeTimers();
  try {
    element.scrollTop = top;
    fireEvent.scroll(element);
    // Complete the virtualizer's scroll-end debounce before jsdom teardown.
    await act(() => vi.runOnlyPendingTimersAsync());
  } finally {
    vi.useRealTimers();
  }
}

it("paginates filtered runs and recovers when live history shrinks", async () => {
  const runs = Object.fromEntries(
    Array.from({ length: 52 }, (_, index) => {
      const runId = `run-${String(index).padStart(3, "0")}`;
      return [
        runId,
        RunSummaryMsg.create({ ...summary, runId, createdSequence: String(index + 1) }),
      ];
    }),
  );
  const onSelectRun = vi.fn();
  const props = { expanded: true, workflowId: workflow.workflowId, runs, onSelectRun };
  const view = render(<RunListPanel {...props} />);
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  await screen.findByRole("button", { name: /run-051,/ });
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  await screen.findByRole("button", { name: /run-026,/ });
  expect(screen.queryByRole("button", { name: /run-051,/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  fireEvent.click(await screen.findByRole("button", { name: /run-000,/ }));
  expect(onSelectRun).toHaveBeenLastCalledWith("run-000");
  expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

  fireEvent.click(screen.getByText("Advanced filters"));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "run-051" } });
  await screen.findByRole("button", { name: /run-051,/ });
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "missing" } });
  expect(screen.getByText("No matching runs")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  view.rerender(<RunListPanel {...props} runs={{ "run-051": runs["run-051"] }} />);
  await screen.findByRole("button", { name: /run-051,/ });
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  view.rerender(<RunListPanel {...props} />);
  await screen.findByRole("button", { name: /run-051,/ });
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
});

it("combines exact status, run ID and date filters without losing hidden active filters", async () => {
  const runs = Object.fromEntries(
    [
      ["failed-in-name", "success", 9],
      ["failed-earlier", "failed", 9],
      ["failed-later", "failed", 10],
    ].map(([runId, status, day], index) => [
      runId,
      RunSummaryMsg.create({
        ...summary,
        runId: String(runId),
        status: String(status),
        createdSequence: String(index + 1),
        triggeredAt: new Date(2026, 8, Number(day), 12).getTime() / 1000,
      }),
    ]),
  );
  const onSelectRun = vi.fn();
  const props = { expanded: true, workflowId: workflow.workflowId, runs, onSelectRun };
  const view = render(<RunListPanel {...props} />);
  expect(screen.getByRole("searchbox")).not.toBeVisible();
  fireEvent.change(screen.getByRole("combobox", { name: "Status" }), {
    target: { value: "failed" },
  });
  await screen.findByRole("button", { name: /failed-later,/ });
  expect(screen.queryByRole("button", { name: /failed-in-name,/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-10" } });
  expect(screen.queryByRole("button", { name: /failed-earlier,/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Advanced filters"));
  fireEvent.change(screen.getByRole("searchbox", { name: "Run ID" }), {
    target: { value: "later" },
  });
  fireEvent.click(screen.getByText("Advanced filters (1 active)"));
  expect(screen.getByRole("searchbox")).not.toBeVisible();
  fireEvent.click(await screen.findByRole("button", { name: /failed-later,/ }));
  expect(onSelectRun).toHaveBeenLastCalledWith("failed-later");

  view.rerender(
    <RunListPanel {...props} runs={{ "failed-in-name": runs["failed-in-name"] }} />,
  );
  expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("failed");
  expect(screen.getByText("No matching runs")).toBeInTheDocument();
});

it("orders the timeline newest first and keeps one workflow or run selected", async () => {
  const runs = Object.fromEntries(
    [8, 10, 9].map((sequence) => {
      const runId = `run-${sequence}`;
      return [
        runId,
        RunSummaryMsg.create({ ...summary, runId, createdSequence: String(sequence) }),
      ];
    }),
  );
  function Timeline() {
    const [selectedRunId, onSelectRun] = useState<string>();
    return (
      <RunListPanel
        workflowId={workflow.workflowId}
        runs={runs}
        selectedRunId={selectedRunId}
        onSelectRun={onSelectRun}
      />
    );
  }
  render(<Timeline />);
  await screen.findByRole("button", { name: /run-8,/ });
  const buttons = screen.getAllByRole("button");
  expect(
    buttons.map(
      (button) => button.getAttribute("aria-label")?.split(",")[0] ?? button.textContent,
    ),
  ).toEqual(["Current", "run-10", "run-9", "run-8"]);
  expect(screen.getAllByRole("button", { pressed: true })).toEqual([buttons[0]]);
  fireEvent.click(buttons[2]);
  expect(screen.getAllByRole("button", { pressed: true })).toEqual([buttons[2]]);
  fireEvent.click(buttons[0]);
  expect(screen.getAllByRole("button", { pressed: true })).toEqual([buttons[0]]);
});

it("loads successive compact pages near the bottom and pins Current above the scrolling runs", async () => {
  const runs = Object.fromEntries(
    Array.from({ length: 52 }, (_, index) => {
      const runId = `run-${index}`;
      return [
        runId,
        RunSummaryMsg.create({ ...summary, runId, createdSequence: String(index + 1) }),
      ];
    }),
  );
  const onViewAll = vi.fn();
  const onSelectRun = vi.fn();
  const props = { workflowId: workflow.workflowId, runs, onSelectRun, onViewAll };
  const view = render(<RunListPanel {...props} />);
  await screen.findByRole("button", { name: /run-51,/ });
  const scroll = view.container.querySelector<HTMLElement>(".run-list-scroll")!;
  const virtual = view.container.querySelector<HTMLElement>(".run-list-virtual")!;
  const current = screen.getByRole("button", { name: "Current" });
  expect(current).toBeVisible();
  expect(scroll).not.toContainElement(current);
  expect(current).toHaveAttribute("aria-pressed", "true");
  expect(current).toHaveStyle({ height: "30px", paddingTop: "7px", paddingBottom: "7px" });
  expect(virtual).toHaveStyle({ height: `${25 * 27.5}px` });
  expect(view.container.querySelector("footer")).not.toBeInTheDocument();
  const viewAll = screen.getByRole("button", { name: "View all" });
  expect(viewAll.closest("header")).not.toBeNull();
  expect(within(scroll).queryByRole("button", { name: "View all" })).not.toBeInTheDocument();
  expect(viewAll).toHaveAttribute("aria-label", "Expand timeline");
  fireEvent.click(within(viewAll).getByText("View all"));
  fireEvent.click(viewAll.querySelector("svg")!);
  expect(onViewAll).toHaveBeenCalledTimes(2);

  vi.useFakeTimers();
  try {
    scroll.scrollTop = 25 * 27.5 - 192 - 27.5;
    fireEvent.scroll(scroll);
    fireEvent.scroll(scroll);
    expect(within(scroll).getByRole("status")).toHaveTextContent("Loading");
    expect(virtual).toHaveStyle({ height: `${25 * 27.5}px` });
    await act(() => vi.runOnlyPendingTimersAsync());
    expect(virtual).toHaveStyle({ height: `${50 * 27.5}px` });
    expect(within(scroll).queryByRole("status")).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
  await scrollTimelineTo(scroll, 50 * 27.5 - 192);
  expect(virtual).toHaveStyle({ height: `${52 * 27.5}px` });
  await scrollTimelineTo(scroll, 52 * 27.5 - 192);
  fireEvent.click(await within(scroll).findByRole("button", { name: /run-0,/ }));
  expect(onSelectRun).toHaveBeenLastCalledWith("run-0");
  expect(current).toBeVisible();
  fireEvent.click(current);
  expect(onSelectRun).toHaveBeenLastCalledWith(undefined);
  expect(within(scroll).queryByRole("status")).not.toBeInTheDocument();
  expect(virtual).toHaveStyle({ height: `${52 * 27.5}px` });

  view.rerender(<RunListPanel {...props} expanded />);
  await scrollTimelineTo(scroll, 25 * 34 - 192);
  expect(await within(scroll).findByRole("button", { name: /run-27,/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Current" })).toHaveStyle({
    height: "36.5px",
    paddingTop: "9.75px",
    paddingBottom: "9.75px",
  });
  expect(scroll).not.toContainElement(current);
  expect(screen.queryByRole("button", { name: "View all" })).not.toBeInTheDocument();
});

it("preserves compact timeline scrolling across status and unrelated workflow updates", async () => {
  const runs = Object.fromEntries(
    Array.from({ length: 20 }, (_, index) => {
      const runId = `run-${index}`;
      return [
        runId,
        RunSummaryMsg.create({ ...summary, runId, createdSequence: String(index + 1) }),
      ];
    }),
  );
  const props = {
    workflowId: workflow.workflowId,
    runs,
    selectedRunId: "run-19",
    onSelectRun: vi.fn(),
    onViewAll: vi.fn(),
  };
  const view = render(<RunListPanel {...props} />);
  await screen.findByRole("button", { name: /run-19,/ });
  const scroll = view.container.querySelector<HTMLElement>(".run-list-scroll")!;
  const bottom = 20 * 27.5 - 192;
  await scrollTimelineTo(scroll, bottom);
  expect(await screen.findByRole("button", { name: "View all" })).toBeVisible();
  expect(within(scroll).queryByRole("button", { name: "View all" })).not.toBeInTheDocument();

  const updatedRuns = {
    ...runs,
    "run-19": RunSummaryMsg.create({ ...runs["run-19"], status: "success", revision: "2" }),
  };
  view.rerender(<RunListPanel {...props} runs={updatedRuns} />);
  expect(scroll.scrollTop).toBe(bottom);

  const otherRun = RunSummaryMsg.create({
    ...summary,
    runId: "other-run",
    workflowId: "other.py::workflow",
    createdSequence: "21",
  });
  view.rerender(
    <RunListPanel {...props} runs={{ ...updatedRuns, [otherRun.runId]: otherRun }} />,
  );
  expect(scroll.scrollTop).toBe(bottom);
  fireEvent.click(screen.getByRole("button", { name: "View all" }));
  expect(props.onViewAll).toHaveBeenCalledOnce();

  const newestRun = RunSummaryMsg.create({
    ...summary,
    runId: "run-20",
    createdSequence: "22",
  });
  const withNewRun = { ...updatedRuns, [newestRun.runId]: newestRun };
  view.rerender(<RunListPanel {...props} runs={withNewRun} />);
  expect(scroll.scrollTop).toBe(bottom);
  view.rerender(<RunListPanel {...props} runs={withNewRun} selectedRunId={newestRun.runId} />);
  expect(scroll.scrollTop).toBe(0);
});

it.each([
  [42.25, "42.3s"],
  [142.3, "2m 22s"],
  [4140.1, "1h 9m 0s"],
])("shows a %s second run duration as %s with total seconds on hover", (seconds, label) => {
  const run = RunSummaryMsg.create({ ...summary, startedAt: 100, endedAt: 100 + seconds });
  render(
    <RunListPanel
      workflowId={workflow.workflowId}
      runs={{ [run.runId]: run }}
      onSelectRun={vi.fn()}
    />,
  );
  const row = screen.getByRole("button", { name: new RegExp(`^${run.runId}, .*, ${label},`) });
  expect(within(row).getByText(label)).toHaveAttribute(
    "title",
    `${seconds.toFixed(1)} seconds`,
  );
  const timestamp = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(run.startedAt * 1000));
  expect(row.querySelector(".run-triggered-at")?.parentElement).toHaveTextContent(
    `${timestamp} · ${label}`,
  );
  expect(row).toHaveStyle({ height: "27.5px", paddingTop: "8.25px", paddingBottom: "8.25px" });
  expect(row).toHaveAttribute("title", run.runId);
  expect(row.querySelector(".run-status-text")).toHaveTextContent(run.status);
  expect(row.querySelector(".run-status-text")).toHaveAttribute("data-status", run.status);
  expect(row.querySelector(".run-status-glyph")).toHaveAttribute("data-glyph", "running");
  expect(row.querySelector(".run-status-text")).not.toHaveClass("border", "rounded");
});
