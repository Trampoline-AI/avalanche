import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Explorer } from "./Explorer";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
  ScanTargetMsg,
} from "./generated/operator";

const workflow = FlowInfoMsg.create({
  workflowId: "flows.py::orders",
  displayName: "Orders",
  rootAlias: "examples",
  relativeFile: "flows.py",
  nodeIds: ["fetch"],
  graph: { fetch: { children: [] } },
  nodeTypes: { fetch: "source" },
  displayNames: { fetch: "Fetch" },
});
const run = RunSnapshotMsg.create({
  summary: RunSummaryMsg.create({
    runId: "run-1",
    workflowId: workflow.workflowId,
    workflowDisplayName: workflow.displayName,
    status: "success",
    startedAt: 1,
    createdSequence: "4",
  }),
});
const target = ScanTargetMsg.create({
  alias: "examples",
  targetPath: "/workspace/examples",
  kind: "directory",
});


describe("Explorer", () => {
  it("navigates the scan-target workflow and historical run hierarchy", () => {
    const onSelect = vi.fn();
    render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "3",
          workflows: [workflow],
          scanTargets: [target],
        })}
        runs={{ "run-1": run }}
        selection={{ kind: "workflow", workflowId: workflow.workflowId }}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("/workspace/examples")).toBeInTheDocument();
    expect(screen.getByText("catalog r3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Ordersflows.py/ }));
    expect(onSelect).toHaveBeenLastCalledWith({
      kind: "workflow",
      workflowId: workflow.workflowId,
    });

    fireEvent.click(screen.getByRole("button", { name: /run-1Created at sequence 4/ }));
    expect(onSelect).toHaveBeenLastCalledWith({
      kind: "run",
      workflowId: workflow.workflowId,
      runId: "run-1",
    });
  });
});
