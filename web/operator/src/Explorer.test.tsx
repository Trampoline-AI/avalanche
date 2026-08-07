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

  it("hides skipped diagnostics and dismisses the current reload notice", () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "5",
          workflows: [orders],
          diagnostics: [
            {
              kind: "skipped",
              path: "helper.py",
              message: "No workflows discovered in this file.",
            },
            { kind: "import_error", path: "flows.py", message: "SyntaxError: invalid syntax" },
          ],
        })}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("1 reload issue")).toBeInTheDocument();
    expect(screen.queryByText("No workflows discovered in this file.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss reload issues" }));
    expect(screen.queryByText("1 reload issue")).not.toBeInTheDocument();

    rerender(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "5",
          workflows: [orders],
          diagnostics: [{ kind: "build_error", path: "flows.py", message: "ValueError: invalid flow" }],
        })}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText("1 reload issue")).toBeInTheDocument();
    expect(screen.getByText("build error")).toBeInTheDocument();
  });
});
