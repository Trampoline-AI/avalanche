import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";

import { RunListPanel } from "./RunListPanel";
import { RunSummaryMsg } from "./model";
import { summary, workflow } from "./test/fixtures";
import type { WorkspaceDeployments } from "./WorkflowWorkspace";

const deploymentItems = [
  { id: "dpl-current-full-id", label: "Latest", createdAt: 1793200000, current: true },
  { id: "dpl-previous-full-id", label: "Previous", createdAt: 1793100000, current: false },
];
const timelineProps = { workflowId: workflow.workflowId, runs: {}, onSelectRun: vi.fn() };

it("hides the pinned current row on every tab when showCurrentRow is false", () => {
  render(
    <RunListPanel
      {...timelineProps}
      showCurrentRow={false}
      currentLabel="Current deployment"
      deployments={{
        items: deploymentItems,
        hasMore: false,
        loading: false,
        onSelect: vi.fn(),
      }}
    />,
  );
  expect(screen.queryByRole("button", { name: "Current deployment" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Deployments" }));
  expect(screen.queryByRole("button", { name: "Current deployment" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^dpl-current-full-id,/ })).toHaveAttribute(
    "aria-current",
    "true",
  );
});

it("defaults to Current definition without a caret, action or tabs", () => {
  render(<RunListPanel {...timelineProps} />);
  const current = screen.getByRole("button", { name: "Current definition" });
  expect(current.querySelector(".lucide-chevron-right")).toBeNull();
  expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  const panel = screen.getByRole("region", { name: "Timeline" });
  expect(within(panel).queryByRole("heading")).not.toBeInTheDocument();
  const runs = within(panel).getByText("Overview");
  expect(runs.tagName).toBe("SPAN");
  expect(runs).toHaveClass("font-semibold", "text-ink", "after:bg-acid");
  expect(runs).not.toHaveAttribute("tabindex");
  expect(screen.getAllByRole("button")).toEqual([current]);
});

it.each([false, true])(
  "hosts an independent Current action and uncontrolled tabs (expanded=%s)",
  async (expanded) => {
    const onSelectRun = vi.fn();
    const onClick = vi.fn();
    const onTimelineViewChange = vi.fn();
    const onSelect = vi.fn();
    const deployments: WorkspaceDeployments = {
      items: deploymentItems,
      hasMore: false,
      loading: false,
      selectedId: deploymentItems[1].id,
      onSelect,
    };
    const view = render(
      <RunListPanel
        {...timelineProps}
        expanded={expanded}
        onSelectRun={onSelectRun}
        currentLabel="Current deployment"
        currentRowAction={{ label: "See all", onClick }}
        deployments={deployments}
        onTimelineViewChange={onTimelineViewChange}
      />,
    );
    const current = screen.getByRole("button", { name: "Current deployment" });
    expect(view.container.querySelector(".run-list-columns > :last-child")).toHaveClass(
      "text-right",
    );
    const action = screen.getByRole("button", { name: "see all" });
    expect(current).not.toContainElement(action);
    // The decoration sits on the painted text, not the button box.
    const actionLabel = action.querySelector(".current-row-action-label")!;
    expect(actionLabel).toHaveClass("underline", "group-hover:no-underline", "font-normal");
    expect(action).toHaveClass("group");
    expect(action).not.toHaveClass("border", "hover:bg-canvas");
    fireEvent.click(action);
    expect(onClick).toHaveBeenCalledOnce();
    expect(onSelectRun).not.toHaveBeenCalled();
    const tabs = screen.getByRole("tablist", { name: "Timeline view" });
    const runsTab = within(tabs).getByRole("tab", { name: "Overview" });
    const deploymentsTab = within(tabs).getByRole("tab", { name: "Deployments" });
    expect(screen.queryByRole("heading", { name: "Timeline" })).not.toBeInTheDocument();
    expect(runsTab).toHaveAttribute("aria-selected", "true");
    expect(runsTab).toHaveAttribute("tabindex", "0");
    expect(runsTab).toHaveClass("font-semibold", "text-ink", "after:bg-acid");
    expect(deploymentsTab).toHaveAttribute("aria-selected", "false");
    expect(deploymentsTab).toHaveAttribute("tabindex", "-1");
    expect(deploymentsTab).toHaveClass("text-run-status-muted");
    expect(deploymentsTab).not.toHaveClass("after:bg-acid");
    const tabPanel = screen.getByRole("tabpanel", { name: "Overview" });
    expect(runsTab).toHaveAttribute("aria-controls", tabPanel.id);
    expect(tabPanel).toHaveAttribute("aria-labelledby", runsTab.id);
    runsTab.focus();
    for (const key of ["ArrowRight", "ArrowLeft", "End"]) {
      fireEvent.keyDown(runsTab, { key });
      expect(deploymentsTab).toHaveFocus();
      expect(deploymentsTab).toHaveAttribute("aria-selected", "true");
      expect(deploymentsTab).toHaveAttribute("tabindex", "0");
      expect(runsTab).toHaveAttribute("tabindex", "-1");
      expect(screen.getByRole("tabpanel", { name: "Deployments" })).toHaveAttribute(
        "id",
        deploymentsTab.getAttribute("aria-controls"),
      );
      fireEvent.keyDown(deploymentsTab, { key: key === "End" ? "Home" : "ArrowRight" });
      expect(runsTab).toHaveFocus();
      expect(runsTab).toHaveAttribute("aria-selected", "true");
    }
    fireEvent.click(deploymentsTab);
    expect(onTimelineViewChange).toHaveBeenLastCalledWith("deployments");
    expect(screen.getByRole("tab", { name: "Deployments" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByRole("group", { name: "Filter by status" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Current deployment" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "see all" })).not.toBeInTheDocument();
    const previous = await screen.findByRole("button", { name: /^dpl-previous-full-id,/ });
    expect(previous).toHaveAttribute("title", deploymentItems[1].id);
    expect(previous).toHaveAttribute("aria-pressed", "true");
    expect(previous).toHaveClass("bg-selected");
    expect(previous).toHaveTextContent("Previous");
    expect(previous.querySelector("time")).toHaveAttribute(
      "dateTime",
      new Date(deploymentItems[1].createdAt * 1000).toISOString(),
    );
    const currentDeployment = screen.getByRole("button", { name: /^dpl-current-full-id,/ });
    expect(currentDeployment).toHaveAttribute("aria-current", "true");
    expect(document.querySelector(".deployment-list-columns, [aria-hidden='true'] .truncate.normal-case")?.textContent).toBe("id");
    expect(currentDeployment.parentElement).toHaveTextContent(/current\s*●/);
    expect(currentDeployment).toHaveClass("text-left");
    expect(currentDeployment.nextElementSibling).toHaveClass("justify-end", "text-right");
    expect(view.container.querySelector(".deployment-list-status")).toHaveClass(
      "justify-end",
      "text-right",
    );
    expect(
      within(currentDeployment.parentElement!).queryByRole("button", { name: "set current" }),
    ).not.toBeInTheDocument();
    fireEvent.click(currentDeployment);
    expect(onSelect).toHaveBeenLastCalledWith(deploymentItems[0].id);
    fireEvent.click(previous);
    expect(onSelect).toHaveBeenLastCalledWith(deploymentItems[1].id);
    expect(onSelectRun).not.toHaveBeenCalled();
    expect(view.container.querySelector(".run-list-scroll")).not.toContainElement(current);
    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(onTimelineViewChange).toHaveBeenLastCalledWith("runs");
    const restoredCurrent = screen.getByRole("button", { name: "Current deployment" });
    expect(screen.getByRole("button", { name: "see all" })).toBeVisible();
    fireEvent.click(restoredCurrent);
    expect(onSelectRun).toHaveBeenLastCalledWith(undefined);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    if (expanded) expect(screen.getByRole("group", { name: "Filter by status" })).toBeVisible();
  },
);

it("keeps the timeline tab controlled and falls back to runs when deployments are removed", () => {
  const onTimelineViewChange = vi.fn();
  const deployments = { items: [], hasMore: false, loading: false, onSelect: vi.fn() };
  const props = { ...timelineProps, deployments, onTimelineViewChange };
  const view = render(<RunListPanel {...props} timelineView="runs" />);
  fireEvent.keyDown(screen.getByRole("tab", { name: "Overview" }), { key: "ArrowRight" });
  expect(screen.getByRole("tab", { name: "Deployments" })).toHaveFocus();
  expect(onTimelineViewChange).toHaveBeenCalledWith("deployments");
  expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  view.rerender(<RunListPanel {...props} timelineView="deployments" />);
  expect(screen.getByText("No deployments")).toBeVisible();
  view.rerender(<RunListPanel {...props} deployments={undefined} timelineView="deployments" />);
  expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  expect(screen.getByText("No runs")).toBeVisible();
});

it.each([false, true])(
  "virtualizes deployments and requests each next page once near the bottom (expanded=%s)",
  async (expanded) => {
    const items = Array.from({ length: 100 }, (_, index) => ({
      ...deploymentItems[1],
      id: `dpl-${index}`,
      label: `Deployment ${index}`,
    }));
    const loadMore = vi.fn();
    const deployments = { items, hasMore: true, loading: false, loadMore, onSelect: vi.fn() };
    const props = {
      ...timelineProps,
      expanded,
      timelineView: "deployments" as const,
      deployments,
    };
    const view = render(<RunListPanel {...props} />);
    await screen.findByRole("button", { name: /^dpl-0,/ });
    expect(screen.getAllByRole("button").length).toBeLessThan(30);
    expect(screen.queryByRole("button", { name: /^dpl-99,/ })).not.toBeInTheDocument();
    const scroll = view.container.querySelector<HTMLElement>(".deployment-list-scroll")!;
    const height = expanded ? 34 : 27.5;
    await scrollTimelineTo(scroll, items.length * height - 192 - height);
    fireEvent.scroll(scroll);
    expect(loadMore).toHaveBeenCalledOnce();
    view.rerender(<RunListPanel {...props} deployments={{ ...deployments }} />);
    fireEvent.scroll(scroll);
    expect(loadMore).toHaveBeenCalledOnce();
    view.rerender(<RunListPanel {...props} deployments={{ ...deployments, loading: true }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading deployments");
    fireEvent.scroll(scroll);
    expect(loadMore).toHaveBeenCalledOnce();
    view.rerender(
      <RunListPanel
        {...props}
        deployments={{ ...deployments, items: [...items, { ...items[0], id: "dpl-100" }] }}
      />,
    );
    await scrollTimelineTo(scroll, 101 * height - 192);
    expect(loadMore).toHaveBeenCalledTimes(2);
    view.rerender(
      <RunListPanel
        {...props}
        deployments={{ ...deployments, hasMore: false, error: "Cannot load deployments" }}
      />,
    );
    fireEvent.scroll(scroll);
    expect(loadMore).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("alert")).toHaveTextContent("Cannot load deployments");
  },
);

it.each([
  { result: "success", label: undefined },
  { result: "failure", label: "Use deployment" },
])(
  "confirms set current inline with pending and $result without selecting a row",
  async ({ result, label }) => {
    const pending = Promise.withResolvers<void>();
    const onConfirm = vi.fn(() => pending.promise);
    const onSelect = vi.fn();
    const deployments = {
      items: [
        ...deploymentItems,
        { ...deploymentItems[1], id: "dpl-older-full-id", label: "Older" },
      ],
      hasMore: false,
      loading: false,
      onSelect,
      setCurrent: {
        label,
        confirmText: (item: (typeof deploymentItems)[number]) =>
          `Use ${item.label} for new runs?`,
        onConfirm,
      },
    };
    const view = render(
      <RunListPanel {...timelineProps} deployments={deployments} timelineView="deployments" />,
    );
    const actions = await screen.findAllByRole("button", { name: (label ?? "set current").toLowerCase() });
    expect(actions).toHaveLength(2);
    const [action] = actions;
    const previous = screen.getByRole("button", { name: /^dpl-previous-full-id,/ });
    expect(previous).not.toContainElement(action);
    const current = screen.getByRole("button", { name: /^dpl-current-full-id,/ });
    expect(current).toHaveAttribute("aria-current", "true");
    expect(current.parentElement).toHaveTextContent(/current\s*●/);
    expect(
      within(current.parentElement!).queryByRole("button", { name: (label ?? "set current").toLowerCase() }),
    ).not.toBeInTheDocument();
    expect(action).toBeVisible();
    expect(action).toBeEnabled();
    expect(action).toHaveAttribute("type", "button");
    expect(action).toHaveClass(
      "border-0",
      "bg-transparent",
      "p-0",
      "text-secondary",
      "group",
      "font-normal",
      "focus-visible:outline-acid",
    );
    expect(action).not.toHaveClass("border", "border-line", "uppercase", "inline-flex", "hover:underline");
    const actionLabel = action.querySelector(".deployment-set-current-label")!;
    expect(actionLabel).toHaveClass("underline", "group-hover:no-underline", "font-normal");
    expect(actionLabel).not.toHaveClass("truncate");
    // Same link style as the Current row action; text only, no icon at rest.
    expect(action.querySelector("svg")).not.toBeInTheDocument();
    expect(action.textContent).toBe((label ?? "set current").toLowerCase());
    const marker = current.parentElement!.querySelector(".deployment-current-marker")!;
    expect(marker.tagName).toBe("SPAN");
    expect(marker).not.toHaveClass("underline");
    expect(marker.textContent?.trim()).toMatch(/^current\s*●$/);
    expect(action.parentElement).toHaveClass("justify-end", "text-right");
    fireEvent.click(action);
    expect(action).toBeDisabled();
    expect(action).toHaveAttribute("aria-busy", "true");
    expect(action.querySelector(".lucide-loader-circle")).toBeInTheDocument();
    expect(actions[1]).toBeDisabled();
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByText("Use Previous for new runs?")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(action).toBeEnabled();
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(action);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalledExactlyOnceWith(deploymentItems[1].id);
    expect(screen.getByRole("button", { name: "Setting current…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(action).toBeDisabled();
    expect(action).toHaveAttribute("aria-busy", "true");
    expect(actions[1]).toBeDisabled();
    if (result === "success") {
      await act(async () => pending.resolve());
      expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
      // The host owns the pointer refresh; apply its returned deployment state.
      view.rerender(
        <RunListPanel
          {...timelineProps}
          deployments={{
            ...deployments,
            items: deployments.items.map((item) => ({
              ...item,
              current: item.id === deploymentItems[1].id,
            })),
          }}
          timelineView="deployments"
        />,
      );
      expect(previous).toHaveAttribute("aria-current", "true");
      expect(previous.parentElement).toHaveTextContent(/current\s*●/);
      expect(
        within(previous.parentElement!).queryByRole("button", { name: "set current" }),
      ).not.toBeInTheDocument();
      expect(current).not.toHaveAttribute("aria-current");
      expect(
        within(current.parentElement!).getByRole("button", { name: "set current" }),
      ).toBeEnabled();
    } else {
      await act(async () =>
        pending.reject(new Error("The current deployment changed; reload and retry.")),
      );
      expect(screen.getByRole("alert")).toHaveTextContent(
        "The current deployment changed; reload and retry.",
      );
      expect(screen.getByRole("button", { name: "Confirm" })).toBeEnabled();
      expect(action).toBeDisabled();
      expect(action).toHaveAttribute("aria-busy", "true");
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(action).toBeEnabled();
    }
    expect(onSelect).not.toHaveBeenCalled();
  },
);

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

  const statusGroup = screen.getByRole("group", { name: "Filter by status" });
  const running = within(statusGroup).getByRole("button", { name: "running 52" });
  fireEvent.click(running);
  expect(running).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  await screen.findByRole("button", { name: /run-051,/ });
  expect(view.container.querySelector(".run-list-scroll")!.scrollTop).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  fireEvent.click(running);
  expect(within(statusGroup).getByRole("button", { name: "All 52" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  await screen.findByRole("button", { name: /run-051,/ });

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
  expect(screen.queryByRole("combobox", { name: "Status" })).not.toBeInTheDocument();
  const statusGroup = screen.getByRole("group", { name: "Filter by status" });
  // Label on its own line; every chip on one non-wrapping row under it.
  expect(statusGroup).toHaveClass("flex-col");
  const chipRow = statusGroup.querySelector<HTMLElement>(".status-chip-row")!;
  expect(chipRow).toHaveClass("flex-nowrap", "overflow-x-auto");
  expect(within(chipRow).getAllByRole("button")).toHaveLength(
    within(statusGroup).getAllByRole("button").length,
  );
  expect(
    within(statusGroup)
      .getAllByRole("button")
      .map((chip) => chip.textContent),
  ).toEqual(["All 3", "failed 2", "success 1"]);
  const all = within(statusGroup).getByRole("button", { name: "All 3" });
  const failed = within(statusGroup).getByRole("button", { name: "failed 2" });
  expect(all).toHaveAttribute("aria-pressed", "true");
  expect(failed).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(failed);
  expect(failed).toHaveAttribute("aria-pressed", "true");
  expect(all).toHaveAttribute("aria-pressed", "false");
  expect(within(statusGroup).getAllByRole("button", { pressed: true })).toEqual([failed]);
  expect(failed).toHaveClass("rounded", "border-acid", "bg-selected", "text-acid");
  fireEvent.click(failed);
  expect(all).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(statusGroup).getByRole("button", { name: "success 1" }));
  expect(failed).toHaveAttribute("aria-pressed", "false");
  expect(screen.queryByRole("button", { name: /failed-later,/ })).not.toBeInTheDocument();
  fireEvent.click(all);
  expect(all).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(failed);
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
  expect(within(statusGroup).getByRole("button", { name: "failed 0" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(within(statusGroup).getByRole("button", { name: "All 1" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  expect(screen.getByText("No matching runs")).toBeInTheDocument();
  fireEvent.click(within(statusGroup).getByRole("button", { name: "failed 0" }));
  expect(within(statusGroup).getByRole("button", { name: "All 1" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByLabelText("From")).toHaveValue("2026-09-10");
  expect(screen.getByRole("searchbox")).toHaveValue("later");
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
  ).toEqual(["Current definition", "run-10", "run-9", "run-8"]);
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
  const current = screen.getByRole("button", { name: "Current definition" });
  expect(current).toBeVisible();
  expect(scroll).not.toContainElement(current);
  expect(current).toHaveAttribute("aria-pressed", "true");
  expect(current).toHaveStyle({ height: "30px", paddingTop: "7px", paddingBottom: "7px" });
  expect(virtual).toHaveStyle({ height: `${25 * 27.5}px` });
  expect(view.container.querySelector("footer")).not.toBeInTheDocument();
  const viewAll = screen.getByRole("button", { name: "Expand timeline" });
  expect(viewAll.closest("header")).not.toBeNull();
  expect(
    within(scroll).queryByRole("button", { name: "Expand timeline" }),
  ).not.toBeInTheDocument();
  expect(viewAll).toHaveAttribute("aria-label", "Expand timeline");
  expect(viewAll).toHaveTextContent("");
  expect(screen.queryByText("View all")).not.toBeInTheDocument();
  fireEvent.click(viewAll);
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
  expect(screen.getByRole("button", { name: "Current definition" })).toHaveStyle({
    height: "36.5px",
    paddingTop: "9.75px",
    paddingBottom: "9.75px",
  });
  expect(scroll).not.toContainElement(current);
  expect(screen.queryByRole("button", { name: "Expand timeline" })).not.toBeInTheDocument();
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
  expect(await screen.findByRole("button", { name: "Expand timeline" })).toBeVisible();
  expect(
    within(scroll).queryByRole("button", { name: "Expand timeline" }),
  ).not.toBeInTheDocument();

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
  fireEvent.click(screen.getByRole("button", { name: "Expand timeline" }));
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
  expect(row.lastElementChild).toHaveClass("justify-self-end", "text-right");
  expect(row).toHaveClass("text-left");
});
