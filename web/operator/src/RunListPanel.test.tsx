import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";

import { RunListPanel } from "./RunListPanel";
import { RunSummaryMsg } from "./model";
import { summary, workflow } from "./test/fixtures";

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

it("caps the compact timeline at twenty runs, with Current and the history action in the scroll range", async () => {
  const entries = Array.from({ length: 21 }, (_, index) => {
    const runId = `run-${index}`;
    return [
      runId,
      RunSummaryMsg.create({ ...summary, runId, createdSequence: String(index + 1) }),
    ] as const;
  });
  const onViewAll = vi.fn();
  const props = { workflowId: workflow.workflowId, onSelectRun: vi.fn(), onViewAll };
  const view = render(
    <RunListPanel {...props} runs={Object.fromEntries(entries.slice(0, 19))} />,
  );
  await screen.findByRole("button", { name: /run-18,/ });
  const scroll = view.container.querySelector<HTMLElement>(".run-list-scroll")!;
  scroll.scrollTop = 20 * 32 - 192;
  fireEvent.scroll(scroll);
  await within(scroll).findByRole("button", { name: /run-0,/ });
  expect(within(scroll).queryByRole("button", { name: "View all" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Current" })).not.toBeInTheDocument();

  view.rerender(<RunListPanel {...props} runs={Object.fromEntries(entries.slice(0, 20))} />);
  scroll.scrollTop = 22 * 32 - 192;
  fireEvent.scroll(scroll);
  expect(await within(scroll).findByRole("button", { name: "View all" })).toBeInTheDocument();

  view.rerender(<RunListPanel {...props} runs={Object.fromEntries(entries)} />);
  await within(scroll).findByRole("button", { name: /run-1,/ });
  expect(within(scroll).queryByRole("button", { name: /run-0,/ })).not.toBeInTheDocument();
  fireEvent.click(within(scroll).getByRole("button", { name: "View all" }));
  expect(onViewAll).toHaveBeenCalledOnce();

  scroll.scrollTop = 0;
  fireEvent.scroll(scroll);
  expect(await screen.findByRole("button", { name: "Current" })).toBeInTheDocument();
  view.rerender(<RunListPanel {...props} expanded runs={Object.fromEntries(entries)} />);
  scroll.scrollTop = 22 * 56 - 192;
  fireEvent.scroll(scroll);
  expect(await within(scroll).findByRole("button", { name: /run-0,/ })).toBeInTheDocument();
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
  const bottom = 22 * 32 - 192;
  scroll.scrollTop = bottom;
  fireEvent.scroll(scroll);
  await within(scroll).findByRole("button", { name: "View all" });

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
  fireEvent.click(within(scroll).getByRole("button", { name: "View all" }));
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
