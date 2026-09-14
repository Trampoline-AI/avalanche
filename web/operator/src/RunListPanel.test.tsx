import { fireEvent, render, screen } from "@testing-library/react";
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
