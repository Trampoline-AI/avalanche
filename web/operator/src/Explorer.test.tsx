import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 44,
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 120) }, (_, index) => ({
        index,
        size: 44,
        start: index * 44,
      })),
  }),
}));

import { Explorer } from "./Explorer";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
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
const run = RunSummaryMsg.create({
  runId: "run-1",
  workflowId: workflow.workflowId,
  workflowDisplayName: workflow.displayName,
  status: "success",
  startedAt: 1,
  createdSequence: "4",
});
const newerRun = RunSummaryMsg.create({
  runId: "run-2",
  workflowId: workflow.workflowId,
  workflowDisplayName: workflow.displayName,
  status: "running",
  startedAt: 2,
  createdSequence: "9007199254740993",
});
const target = ScanTargetMsg.create({
  alias: "examples",
  targetPath: "/workspace/examples",
  kind: "directory",
});

function countBranchRenders(
  source: FlowInfoMsg,
  counter: { value: number },
): FlowInfoMsg {
  return new Proxy(source, {
    get(target, property, receiver) {
      if (property === "relativeFile") counter.value += 1;
      return Reflect.get(target, property, receiver);
    },
  });
}


describe("Explorer", () => {
  it("navigates the scan-target workflow and historical run hierarchy", async () => {
    const onSelect = vi.fn();
    const view = render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "3",
          workflows: [workflow],
          scanTargets: [target],
        })}
        runs={{ "run-1": run, "run-2": newerRun }}
        selection={{ kind: "workflow", workflowId: workflow.workflowId }}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("/workspace/examples")).toBeInTheDocument();
    expect(screen.getByText("catalog r3")).toBeInTheDocument();
    const runButtons = await screen.findAllByRole("button", { name: /run-\d/ });
    expect(runButtons.map((button) => button.textContent)).toEqual([
      expect.stringContaining("run-2"),
      expect.stringContaining("run-1"),
    ]);

    fireEvent.click(screen.getByRole("button", { name: /Ordersflows.py/ }));
    expect(onSelect).toHaveBeenLastCalledWith({
      kind: "workflow",
      workflowId: workflow.workflowId,
    });

    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 4/ }));
    expect(onSelect).toHaveBeenLastCalledWith({
      kind: "run",
      workflowId: workflow.workflowId,
      runId: "run-1",
    });

    view.rerender(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "3",
          workflows: [workflow],
          scanTargets: [target],
        })}
        runs={{ "run-1": run, "run-2": newerRun }}
        selection={{ kind: "run", workflowId: workflow.workflowId, runId: "run-2" }}
        onSelect={onSelect}
      />,
    );
    expect(
      await screen.findByRole("button", { name: /run-2Created/ }),
    ).toHaveClass("active");
  });

  it("keeps unrelated branches out of parent detail and run-summary rerenders", async () => {
    const ordersRenders = { value: 0 };
    const inventoryRenders = { value: 0 };
    const orders = countBranchRenders(workflow, ordersRenders);
    const inventory = countBranchRenders(
      FlowInfoMsg.create({
        ...workflow,
        workflowId: "flows.py::inventory",
        displayName: "Inventory",
      }),
      inventoryRenders,
    );
    const inventoryRun = RunSummaryMsg.create({
      ...run,
      runId: "inventory-run",
      workflowId: inventory.workflowId,
      workflowDisplayName: inventory.displayName,
    });
    const catalog = {
      ...CatalogSnapshotMsg.create({
        revision: "3",
        scanTargets: [target],
      }),
      workflows: [orders, inventory],
    };
    const runs = { "run-1": run, "inventory-run": inventoryRun };
    const selection = { kind: "workflow", workflowId: orders.workflowId } as const;
    const onSelect = vi.fn();
    const view = render(
      <Explorer
        catalog={catalog}
        runs={runs}
        selection={selection}
        onSelect={onSelect}
      />,
    );

    expect(await screen.findByText("Orders")).toBeInTheDocument();
    expect(screen.getByText("Inventory")).toBeInTheDocument();
    const initialOrdersRenders = ordersRenders.value;
    const initialInventoryRenders = inventoryRenders.value;
    expect(initialOrdersRenders).toBeGreaterThan(0);
    expect(initialInventoryRenders).toBeGreaterThan(0);

    view.rerender(
      <Explorer
        catalog={catalog}
        runs={runs}
        selection={selection}
        onSelect={onSelect}
      />,
    );

    expect(ordersRenders.value).toBe(initialOrdersRenders);
    expect(inventoryRenders.value).toBe(initialInventoryRenders);

    const updatedInventoryRun = { ...inventoryRun, status: "failed" };
    view.rerender(
      <Explorer
        catalog={catalog}
        runs={{ ...runs, "inventory-run": updatedInventoryRun }}
        selection={selection}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByRole("button", { name: /inventory-run/ })).toHaveTextContent("!");
    expect(ordersRenders.value).toBe(initialOrdersRenders);
    expect(inventoryRenders.value).toBeGreaterThan(initialInventoryRenders);
    const inventoryRendersAfterUpdate = inventoryRenders.value;

    const updatedOrderRun = { ...run, status: "failed" };
    view.rerender(
      <Explorer
        catalog={catalog}
        runs={{
          "run-1": updatedOrderRun,
          "inventory-run": updatedInventoryRun,
        }}
        selection={selection}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByRole("button", { name: /run-1/ })).toHaveTextContent("!");
    expect(ordersRenders.value).toBeGreaterThan(initialOrdersRenders);
    expect(inventoryRenders.value).toBe(inventoryRendersAfterUpdate);
  });

});
