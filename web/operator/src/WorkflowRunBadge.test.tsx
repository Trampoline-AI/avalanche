import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Explorer } from "./Explorer";
import { CatalogSnapshotMsg, FlowInfoMsg, RunSummaryMsg } from "./model";
import { runRingShares, type WorkflowRunCounts } from "./WorkflowRunBadge";

describe("run ring proportions", () => {
  it.each([
    [{ total: 0, success: 0, error: 0 }, [0, 0, 1]],
    [{ total: 100, success: 100, error: 0 }, [1, 0, 0]],
    [{ total: 100, success: 0, error: 100 }, [0, 1, 0]],
    [{ total: 100, success: 99, error: 1 }, [0.9, 0.1, 0]],
    [{ total: 100, success: 90, error: 10 }, [0.9, 0.1, 0]],
    [{ total: 100, success: 60, error: 40 }, [0.6, 0.4, 0]],
    [{ total: 100, success: 66, error: 1 }, [0.6, 0.1, 0.3]],
    [{ total: 10, success: 0, error: 0 }, [0, 0, 1]],
    [{ total: 1000, success: 999, error: 1 }, [0.9, 0.1, 0]],
  ] satisfies [WorkflowRunCounts, number[]][])("allocates %j", (counts, expected) => {
    const shares = runRingShares(counts);
    expect(shares.success).toBeCloseTo(expected[0]);
    expect(shares.error).toBeCloseTo(expected[1]);
    expect(shares.neutral).toBeCloseTo(expected[2]);
    expect(shares.success + shares.error + shares.neutral).toBeCloseTo(1);
  });
});

describe("workflow run badges", () => {
  const workflow = FlowInfoMsg.create({
    workflowId: "orders",
    displayName: "Orders",
    cron: "0 * * * *",
    scheduleEnabled: true,
  });
  const catalog = CatalogSnapshotMsg.create({
    workflows: [workflow, FlowInfoMsg.create({ workflowId: "empty", displayName: "Empty" })],
  });
  const run = (runId: string, status: string, workflowId = workflow.workflowId) =>
    RunSummaryMsg.create({ runId, status, workflowId });

  it.each([true, false])("shows per-workflow totals and updates in expanded=%s", (expanded) => {
    const onSelect = vi.fn();
    const runs = {
      success: run("success", "success"),
      failed: run("failed", "failed"),
      running: run("running", "running"),
      cancelled: run("cancelled", "cancelled"),
      waiting: run("waiting", "waiting"),
      unrelated: run("unrelated", "failed", "another-workflow"),
    };
    const { rerender } = render(
      <Explorer catalog={catalog} runs={runs} onSelect={onSelect} expanded={expanded} />,
    );
    const orders = screen.getByRole("button", { name: /Orders/ });
    const badge = within(orders).getByRole("img", {
      name: /5 runs: 1 successful, 1 failed, 3 other/,
    });
    expect(badge).toHaveTextContent("5");
    expect(
      within(screen.getByRole("button", { name: /Empty/ })).getByRole("img"),
    ).toHaveTextContent("0");
    fireEvent.click(badge);
    expect(onSelect).toHaveBeenCalledWith({ kind: "workflow", workflowId: "orders" });

    rerender(
      <Explorer
        catalog={catalog}
        runs={{ ...runs, running: run("running", "failed"), new: run("new", "success") }}
        onSelect={onSelect}
        expanded={expanded}
      />,
    );
    expect(
      within(orders).getByRole("img", { name: /6 runs: 2 successful, 2 failed, 2 other/ }),
    ).toHaveTextContent("6");

    rerender(<Explorer catalog={catalog} runs={{}} onSelect={onSelect} expanded={expanded} />);
    expect(within(orders).getByRole("img", { name: /0 runs/ })).toHaveTextContent("0");
  });
});
