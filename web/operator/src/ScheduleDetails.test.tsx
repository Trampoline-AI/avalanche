import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { FlowInfoMsg, RunSummaryMsg } from "./model";
import { ScheduleDetails } from "./ScheduleDetails";
import { summary, workflow } from "./test/fixtures";

const nightly = FlowInfoMsg.create({
  ...workflow,
  workflowId: "nightly.py::nightly",
  cron: "0 2 * * *",
  scheduleEnabled: true,
  nextRunAt: Date.UTC(2026, 9, 30, 2) / 1000,
  lastRunAt: Date.UTC(2026, 9, 29, 2) / 1000,
});

function run(runId: string, createdSequence: string, overrides: Partial<RunSummaryMsg> = {}) {
  return RunSummaryMsg.create({
    ...summary,
    runId,
    workflowId: nightly.workflowId,
    createdSequence,
    status: "success",
    triggeredBy: "scheduled",
    ...overrides,
  });
}

it("opens the newest scheduled run of this workflow from the last run", () => {
  const onSelectRun = vi.fn();
  const runs = Object.fromEntries(
    [
      run("older-scheduled", "1"),
      run("newest-scheduled", "2", { status: "failed" }),
      run("newer-manual", "3", { triggeredBy: "manual" }),
      run("other-workflow", "4", { workflowId: workflow.workflowId }),
    ].map((item) => [item.runId, item]),
  );
  render(<ScheduleDetails workflow={nightly} runs={runs} onSelectRun={onSelectRun} />);

  const details = screen.getByRole("region", { name: "Schedule" });
  fireEvent.click(within(details).getByRole("button", { name: /newest-scheduled, failed/ }));

  expect(onSelectRun).toHaveBeenCalledWith("newest-scheduled");
  expect(within(details).getAllByRole("button")).toHaveLength(1);
});

it("shows the next run only while the schedule is enabled", () => {
  const { rerender } = render(<ScheduleDetails workflow={nightly} runs={{}} />);
  const details = screen.getByRole("region", { name: "Schedule" });
  // Next and last run.
  expect(within(details).getAllByRole("time")).toHaveLength(2);

  rerender(<ScheduleDetails workflow={{ ...nightly, scheduleEnabled: false }} runs={{}} />);
  expect(within(details).getAllByRole("time")).toHaveLength(1);
  expect(within(details).queryByRole("button")).not.toBeInTheDocument();
});
