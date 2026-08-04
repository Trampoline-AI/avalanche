import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Explorer } from "./Explorer";
import { CatalogSnapshotMsg, FlowInfoMsg } from "./generated/operator";

const orders = FlowInfoMsg.create({
  workflowId: "flows.py::orders",
  displayName: "Orders",
  rootAlias: "examples",
  relativeFile: "flows.py",
  nodeIds: ["fetch"],
  graph: { fetch: { children: [] } },
  nodeTypes: { fetch: "source" },
  displayNames: { fetch: "Fetch" },
});
const inventory = FlowInfoMsg.create({
  ...orders,
  workflowId: "inventory.py::inventory",
  displayName: "Inventory",
  rootAlias: "services",
  relativeFile: "inventory.py",
});

describe("Explorer", () => {
  it("lists scanned workflows directly without target or run branches", () => {
    const onSelect = vi.fn();
    const view = render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "3",
          workflows: [orders, inventory],
          scanTargets: [
            { alias: "examples", targetPath: "/workspace/examples", kind: "directory" },
            { alias: "services", targetPath: "/workspace/services", kind: "directory" },
          ],
        })}
        selection={{ kind: "workflow", workflowId: orders.workflowId }}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("catalog r3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ordersflows.py/ })).toHaveClass("active");
    expect(screen.getByRole("button", { name: /Inventoryinventory.py/ })).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent("/workspace/examples");
    expect(view.container.querySelector(".target-heading")).not.toBeInTheDocument();
    expect(view.container.querySelector(".run-branches")).not.toBeInTheDocument();
    expect(view.container.querySelector(".tree-disclosure")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Inventoryinventory.py/ }));
    expect(onSelect).toHaveBeenCalledWith({
      kind: "workflow",
      workflowId: inventory.workflowId,
    });
  });

  it("shows an explicit empty state when nothing was scanned", () => {
    render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({ revision: "4", workflows: [] })}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("No workflows scanned")).toBeInTheDocument();
  });
});
