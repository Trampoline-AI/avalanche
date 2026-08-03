import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({
    count,
    getScrollElement,
    scrollMargin = 0,
  }: {
    count: number;
    getScrollElement: () => HTMLElement | null;
    scrollMargin?: number;
  }) => {
    const scrollOffset = getScrollElement()?.scrollTop ?? 0;
    const firstIndex = Math.min(
      count,
      Math.max(0, Math.floor((scrollOffset - scrollMargin) / 44)),
    );
    return {
      getTotalSize: () => count * 44,
      getVirtualItems: () =>
        Array.from(
          { length: Math.min(count - firstIndex, 3) },
          (_, offset) => {
            const index = firstIndex + offset;
            return {
              index,
              size: 44,
              start: scrollMargin + index * 44,
            };
          },
        ),
    };
  },
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


afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
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
    const runItems = screen.getAllByRole("listitem");
    expect(runItems[0]).toHaveAttribute("aria-setsize", "2");
    expect(runItems[0]).toHaveAttribute("aria-posinset", "1");
    expect(runItems[1]).toHaveAttribute("aria-setsize", "2");
    expect(runItems[1]).toHaveAttribute("aria-posinset", "2");

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

  it("repositions later virtual branches when an earlier target collapses", () => {
    const earlierTarget = ScanTargetMsg.create({
      alias: "earlier",
      targetPath: "/earlier/path",
      kind: "directory",
    });
    const laterTarget = ScanTargetMsg.create({
      alias: "later",
      targetPath: "/later/path",
      kind: "directory",
    });
    const earlierWorkflow = FlowInfoMsg.create({
      ...workflow,
      workflowId: "flows.py::earlier",
      displayName: "Earlier",
      rootAlias: earlierTarget.alias,
    });
    const laterWorkflow = FlowInfoMsg.create({
      ...workflow,
      workflowId: "flows.py::later",
      displayName: "Later",
      rootAlias: laterTarget.alias,
    });
    const laterRuns = Array.from({ length: 8 }, (_, index) =>
      RunSummaryMsg.create({
        ...run,
        runId: `later-run-${index}`,
        workflowId: laterWorkflow.workflowId,
        workflowDisplayName: laterWorkflow.displayName,
        createdSequence: String(100 - index),
      }),
    );
    const runs = Object.fromEntries(
      laterRuns.map((summary) => [summary.runId, summary]),
    );
    const nativeGetBoundingClientRect = Element.prototype.getBoundingClientRect;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
      function getBoundingClientRect(this: Element) {
        if (
          this.matches(
            '.run-virtual-list[aria-label="Runs for Later"]',
          )
        ) {
          const explorer = document.querySelector(".explorer") as HTMLElement;
          const earlierIsExpanded = screen.queryByText("Earlier") !== null;
          const layoutTop = earlierIsExpanded ? 300 : 100;
          return new DOMRect(
            0,
            layoutTop - explorer.scrollTop,
            280,
            laterRuns.length * 44,
          );
        }
        return nativeGetBoundingClientRect.call(this);
      },
    );

    let notifyContentLayout: (() => void) | undefined;
    const observedElements: Element[] = [];
    let observerCount = 0;
    class SharedContentResizeObserver implements ResizeObserver {
      readonly #callback: ResizeObserverCallback;

      constructor(callback: ResizeObserverCallback) {
        observerCount += 1;
        this.#callback = callback;
        notifyContentLayout = () => this.#callback([], this);
      }

      observe(target: Element) {
        observedElements.push(target);
      }

      unobserve() {}

      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", SharedContentResizeObserver);

    render(
      <Explorer
        catalog={CatalogSnapshotMsg.create({
          revision: "3",
          workflows: [earlierWorkflow, laterWorkflow],
          scanTargets: [earlierTarget, laterTarget],
        })}
        runs={runs}
        onSelect={vi.fn()}
      />,
    );

    expect(observerCount).toBe(1);
    expect(observedElements).toEqual([
      document.querySelector(".target-list"),
    ]);
    const explorer = screen.getByRole("complementary", { name: "Explorer" });
    explorer.scrollTop = 310;
    act(() => notifyContentLayout?.());

    const laterList = screen.getByRole("list", { name: "Runs for Later" });
    expect(within(laterList).getAllByRole("listitem")[0]).toHaveAttribute(
      "aria-posinset",
      "1",
    );

    fireEvent.click(
      screen.getByRole("button", { name: /earlier.*\/earlier\/path/i }),
    );
    act(() => notifyContentLayout?.());

    const visibleRows = within(laterList).getAllByRole("listitem");
    expect(visibleRows[0]).toHaveAttribute("aria-setsize", "8");
    expect(visibleRows[0]).toHaveAttribute("aria-posinset", "5");
    expect(visibleRows[0]).toHaveTextContent("later-run-4");
    const translateY = Number(
      visibleRows[0].style.transform.match(/translateY\((-?\d+)px\)/)?.[1],
    );
    const firstRowTop = laterList.getBoundingClientRect().top + translateY;
    expect(firstRowTop).toBeLessThanOrEqual(0);
    expect(firstRowTop + 44).toBeGreaterThan(0);
  });

});
