import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 32,
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 120) }, (_, index) => ({
        index,
        size: 32,
        start: index * 32,
      })),
  }),
}));

const projectionHarness = vi.hoisted(() => ({
  state: {
    catalog: {
      operatorInstanceId: "operator-1",
      asOfSequence: "1",
      revision: "1",
      workflows: [
        {
          workflowId: "flow.py::demo",
          displayName: "demo",
          rootAlias: "examples",
          relativeFile: "flow.py",
          nodeIds: ["node-1"],
          graph: { "node-1": { children: [] } },
          nodeTypes: { "node-1": "task" },
          displayNames: { "node-1": "Current node" },
          agentNodeIds: [],
          agentMetadataJson: {},
        },
      ],
      scanTargets: [
        {
          alias: "examples",
          targetPath: "/workspace/examples",
          kind: "directory",
        },
      ],
      diagnostics: [],
    },
    runs: {} as Record<string, unknown>,
    selectedRun: undefined as unknown,
    selectedRunId: undefined as string | undefined,
    selectedRunStatus: "idle",
    selectedRunError: undefined as string | undefined,
    liveEvents: {},
    liveLogs: {},
    liveEventRepairWatermarks: {},
    liveLogRepairWatermarks: {},
    operatorInstanceId: "operator-1",
    sequence: "1",
    connection: "live",
    workflowReloading: false,
  },
  selectRun: vi.fn(async (_runId?: string) => undefined),
  startRun: vi.fn(async () => "run-1"),
  cancelRun: vi.fn(async () => undefined),
}));

vi.mock("./GraphCanvas", () => ({
  GraphCanvas: ({
    runTopology,
    topLeftPanel,
    bottomRightPanel,
    onOpenNode,
  }: {
    runTopology?: { displayNames: Record<string, string> };
    topLeftPanel?: ReactNode;
    bottomRightPanel?: ReactNode;
    onOpenNode: (nodeId: string) => void;
  }) => (
    <div>
      <div className="dag-runs-panel">{topLeftPanel}</div>
      <button type="button" onClick={() => onOpenNode("node-1")}>
        {runTopology ? `Run graph ${runTopology.displayNames["node-1"]}` : "Workflow graph"}
      </button>
      <div className="dag-actions-panel">{bottomRightPanel}</div>
    </div>
  ),
}));
vi.mock("./Inspector", () => ({
  Inspector: ({
    run,
    onClose,
  }: {
    run?: { summary?: { runId: string } };
    onClose: () => void;
  }) => (
    <div>
      <span>{`Inspector ${run?.summary?.runId ?? "workflow"}`}</span>
      <button type="button" onClick={onClose}>Close inspector</button>
    </div>
  ),
}));
vi.mock("./RunLogPane", () => ({
  RunLogPane: ({
    nodeId,
    liveLogs,
    onSelectNode,
  }: {
    nodeId?: string;
    liveLogs?: { sequence: string }[];
    onSelectNode: (nodeId: string) => void;
  }) => (
    <section aria-label="Run logs">
      <span>{`Log scope ${nodeId ?? "all"}`}</span>
      <span>{`Live logs ${liveLogs?.map((log) => log.sequence).join(",") ?? "none"}`}</span>
      <button type="button" onClick={() => onSelectNode("node-1")}>Select log node</button>
    </section>
  ),
}));
vi.mock("./state", () => ({
  useOperatorProjection: () => projectionHarness,
}));

import { App } from "./App";
import { GrpcWebOperatorApi } from "./api";
import { RunSnapshotMsg, RunSummaryMsg, WorkflowTopologyMsg } from "./generated/operator";

const summary = RunSummaryMsg.create({
  runId: "run-1",
  workflowId: "flow.py::demo",
  workflowDisplayName: "demo",
  status: "running",
  startedAt: 1,
  createdSequence: "2",
});

function selectedSnapshot(runId: string, nodeName: string) {
  return RunSnapshotMsg.create({
    operatorInstanceId: "operator-1",
    asOfSequence: "2",
    summary: RunSummaryMsg.create({ ...summary, runId }),
    topology: WorkflowTopologyMsg.create({
      nodeIds: ["node-1"],
      graph: { "node-1": { children: [] } },
      nodeTypes: { "node-1": "task" },
      displayNames: { "node-1": nodeName },
    }),
  });
}

describe("App", () => {
  beforeEach(() => {
    projectionHarness.state.runs = {};
    projectionHarness.state.selectedRun = undefined;
    projectionHarness.state.selectedRunId = undefined;
    projectionHarness.state.selectedRunStatus = "idle";
    projectionHarness.state.selectedRunError = undefined;
    projectionHarness.state.liveEvents = {};
    projectionHarness.state.liveLogs = {};
    projectionHarness.state.catalog.revision = "1";
    projectionHarness.state.sequence = "1";
    projectionHarness.state.workflowReloading = false;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1024,
    });
    projectionHarness.state.liveEventRepairWatermarks = {};
    projectionHarness.state.liveLogRepairWatermarks = {};
    projectionHarness.selectRun.mockClear();
    projectionHarness.startRun.mockClear();
    projectionHarness.cancelRun.mockClear();
    projectionHarness.state.connection = "live";
  });

  it("shows a full-screen connecting view until the operator is available", () => {
    projectionHarness.state.connection = "reconnecting";

    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} operatorPort="17777" />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting...");
    expect(screen.getByText("No operator process found at port 17777")).toBeVisible();
    expect(container.querySelector(".operator-connection-screen")).toHaveClass(
      "min-h-screen",
    );
    expect(container.querySelector(".topbar")).not.toBeInTheDocument();
  });

  it("shows a bottom update card while workflows are reloading", () => {
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    projectionHarness.state.workflowReloading = true;
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    const indicator = screen.getByRole("status");
    expect(indicator).toBeVisible();
    expect(indicator).toHaveClass("bottom-5");

    projectionHarness.state.workflowReloading = false;
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps Explorer accessible through the narrow navigation toggle", () => {
    window.innerWidth = 375;
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const toggle = screen.getByRole("button", { name: "Explorer" });

    expect(toggle).toHaveAttribute("aria-controls", "operator-explorer");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("complementary", { name: "Explorer" })).toHaveAttribute(
      "id",
      "operator-explorer",
    );

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelector(".app-shell")).toHaveClass("explorer-open");

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(projectionHarness.startRun).toHaveBeenCalledWith("flow.py::demo", undefined);
  });

  it("starts a run without selecting it before preparation completes", async () => {
    render(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(projectionHarness.startRun).toHaveBeenCalledWith("flow.py::demo", undefined),
    );
    expect(projectionHarness.selectRun).not.toHaveBeenCalled();
  });

  it("collapses and restores the desktop Explorer independently of the narrow toggle", () => {
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const toggle = screen.getByRole("button", { name: "Collapse Explorer" });
    const explorer = screen.getByRole("complementary", { name: "Explorer" });
    const narrowToggle = screen.getByRole("button", { name: "Explorer" });

    expect(toggle).toHaveAttribute("aria-controls", "operator-explorer");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(explorer).toContainElement(toggle);
    expect(screen.getByRole("separator", { name: "Resize Explorer" })).toBeInTheDocument();

    fireEvent.click(toggle);

    expect(container.querySelector(".app-shell")).toHaveClass("explorer-collapsed");
    const restore = screen.getByRole("button", { name: "Restore Explorer" });
    expect(restore).toHaveAttribute("aria-expanded", "false");
    expect(restore.closest(".dag-runs-panel")).not.toBeNull();
    expect(narrowToggle).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("separator", { name: "Resize Explorer" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Explorer" })).toBe(explorer);

    fireEvent.click(restore);

    expect(container.querySelector(".app-shell")).not.toHaveClass("explorer-collapsed");
    expect(screen.getByRole("button", { name: "Collapse Explorer" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(explorer).toContainElement(
      screen.getByRole("button", { name: "Collapse Explorer" }),
    );
    expect(screen.getByRole("separator", { name: "Resize Explorer" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Explorer" })).toBe(explorer);
  });

  it("maps divider arrow keys to physical movement and intended pane widths", () => {
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const workspace = container.querySelector<HTMLElement>(".workspace")!;
    const explorerDivider = screen.getByRole("separator", {
      name: "Resize Explorer",
    });

    expect(explorerDivider).toHaveAttribute("aria-valuenow", "280");
    expect(explorerDivider).toHaveAttribute("aria-valuetext", "280 pixels");

    fireEvent.keyDown(explorerDivider, { key: "ArrowLeft" });
    expect(explorerDivider).toHaveAttribute("aria-valuenow", "264");
    expect(workspace.style.getPropertyValue("--workspace-explorer-width")).toBe(
      "264px",
    );

    fireEvent.keyDown(explorerDivider, { key: "ArrowRight" });
    expect(explorerDivider).toHaveAttribute("aria-valuenow", "280");
    expect(workspace.style.getPropertyValue("--workspace-explorer-width")).toBe(
      "280px",
    );

    fireEvent.click(screen.getByRole("button", { name: "Workflow graph" }));
    const inspectorDivider = screen.getByRole("separator", {
      name: "Resize Inspector",
    });

    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "410");
    expect(inspectorDivider).toHaveAttribute("aria-valuetext", "410 pixels");

    fireEvent.keyDown(inspectorDivider, { key: "ArrowLeft" });
    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "426");
    expect(workspace.style.getPropertyValue("--workspace-inspector-width")).toBe(
      "426px",
    );

    fireEvent.keyDown(inspectorDivider, { key: "ArrowRight" });
    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "410");
    expect(workspace.style.getPropertyValue("--workspace-inspector-width")).toBe(
      "410px",
    );
  });

  it("clamps direction-aware divider keyboard resizing without changing Home or End", () => {
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const workspace = container.querySelector<HTMLElement>(".workspace")!;
    const explorerDivider = screen.getByRole("separator", {
      name: "Resize Explorer",
    });

    expect(explorerDivider).toHaveAttribute("aria-orientation", "vertical");
    expect(explorerDivider).toHaveAttribute("aria-controls", "operator-explorer");
    expect(explorerDivider).toHaveAttribute("aria-valuemin", "220");
    expect(explorerDivider).toHaveAttribute("aria-valuemax", "420");
    fireEvent.keyDown(explorerDivider, { key: "Home" });
    fireEvent.keyDown(explorerDivider, { key: "ArrowLeft" });
    expect(explorerDivider).toHaveAttribute("aria-valuenow", "220");
    expect(workspace.style.getPropertyValue("--workspace-explorer-width")).toBe(
      "220px",
    );
    fireEvent.keyDown(explorerDivider, { key: "End" });
    fireEvent.keyDown(explorerDivider, { key: "ArrowRight" });
    expect(explorerDivider).toHaveAttribute("aria-valuenow", "420");
    expect(workspace.style.getPropertyValue("--workspace-explorer-width")).toBe(
      "420px",
    );

    fireEvent.click(screen.getByRole("button", { name: "Workflow graph" }));
    const inspectorDivider = screen.getByRole("separator", {
      name: "Resize Inspector",
    });

    expect(inspectorDivider).toHaveAttribute("aria-orientation", "vertical");
    expect(inspectorDivider).toHaveAttribute("aria-controls", "operator-inspector");
    expect(inspectorDivider).toHaveAttribute("aria-valuemin", "320");
    expect(inspectorDivider).toHaveAttribute("aria-valuemax", "640");
    fireEvent.keyDown(inspectorDivider, { key: "Home" });
    fireEvent.keyDown(inspectorDivider, { key: "ArrowRight" });
    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "320");
    expect(workspace.style.getPropertyValue("--workspace-inspector-width")).toBe(
      "320px",
    );
    fireEvent.keyDown(inspectorDivider, { key: "End" });
    fireEvent.keyDown(inspectorDivider, { key: "ArrowLeft" });
    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "640");
    expect(workspace.style.getPropertyValue("--workspace-inspector-width")).toBe(
      "640px",
    );
  });

  it("keeps pointer resizing aligned with each divider direction", () => {
    const { container } = render(
      <App api={new GrpcWebOperatorApi("http://localhost")} />,
    );
    const workspace = container.querySelector<HTMLElement>(".workspace")!;
    const explorerDivider = screen.getByRole("separator", {
      name: "Resize Explorer",
    });

    fireEvent.pointerDown(explorerDivider, { pointerId: 1, clientX: 280 });
    fireEvent.pointerMove(explorerDivider, { pointerId: 1, clientX: 344 });
    fireEvent.pointerUp(explorerDivider, { pointerId: 1 });
    expect(explorerDivider).toHaveAttribute("aria-valuenow", "344");
    expect(workspace.style.getPropertyValue("--workspace-explorer-width")).toBe(
      "344px",
    );

    fireEvent.click(screen.getByRole("button", { name: "Workflow graph" }));
    const inspectorDivider = screen.getByRole("separator", {
      name: "Resize Inspector",
    });

    fireEvent.pointerDown(inspectorDivider, { pointerId: 2, clientX: 700 });
    fireEvent.pointerMove(inspectorDivider, { pointerId: 2, clientX: 636 });
    fireEvent.pointerUp(inspectorDivider, { pointerId: 2 });
    expect(inspectorDivider).toHaveAttribute("aria-valuenow", "474");
    expect(workspace.style.getPropertyValue("--workspace-inspector-width")).toBe(
      "474px",
    );
  });

  it("keeps transport sequence internal while log and trace updates retain catalog revision", () => {
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    const catalogRevision = screen.getByText("catalog r1");

    expect(view.container).not.toHaveTextContent(/seq 1/i);
    expect(view.container).not.toHaveTextContent(/sequence 1/i);

    projectionHarness.state.sequence = "93";
    projectionHarness.state.liveLogs = {
      "run-1": [{ sequence: "92" }],
    };
    projectionHarness.state.liveEvents = {
      "run-1:node-1": [{ eventSequence: "93" }],
    };
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    expect(screen.getByText("catalog r1")).toBe(catalogRevision);
    expect(view.container).not.toHaveTextContent(/seq 93/i);
    expect(view.container).not.toHaveTextContent(/sequence 93/i);
  });

  it("removes the canvas header while preserving the retained snapshot explanation", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    expect(view.container.querySelector(".view-header")).not.toBeInTheDocument();
    expect(view.container).not.toHaveTextContent(/Historical run/i);

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    expect(screen.getByText("Immutable run snapshot")).toBeInTheDocument();
    expect(
      screen.getByText("Current workflow changes do not alter this canvas"),
    ).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent(/Historical run/i);
    projectionHarness.selectRun.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Current workflow" }));
    expect(projectionHarness.selectRun).toHaveBeenCalledWith(undefined);
    expect(screen.getByRole("button", { name: "Workflow graph" })).toBeInTheDocument();
    expect(screen.queryByText("Immutable run snapshot")).not.toBeInTheDocument();
  });

  it("navigates summary-only runs with one demand-load selection and clears it", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    expect(projectionHarness.selectRun).toHaveBeenCalledTimes(1);
    expect(projectionHarness.selectRun).toHaveBeenCalledWith("run-1");
    expect(screen.getByRole("heading", { name: "No run snapshot" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /demoflow.py/ }));
    expect(projectionHarness.selectRun).toHaveBeenLastCalledWith(undefined);

    fireEvent.click(screen.getByRole("button", { name: /run-1/ }));
    view.unmount();
    expect(projectionHarness.selectRun).toHaveBeenLastCalledWith(undefined);
  });

  it("shows selected-run loading and error states without rendering stale snapshots", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "loading";
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.getByRole("heading", { name: "Loading run snapshot" })).toBeInTheDocument();

    projectionHarness.state.selectedRunStatus = "error";
    projectionHarness.state.selectedRunError = "snapshot was evicted";
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("snapshot was evicted");

    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-2", "Stale node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.queryByText(/Run graph/)).not.toBeInTheDocument();

    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(screen.getByRole("button", { name: "Run graph Recorded node" }));
    expect(screen.getByText("Inspector run-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(projectionHarness.cancelRun).toHaveBeenCalledWith("run-1");

    projectionHarness.state.selectedRunId = "run-2";
    projectionHarness.state.selectedRun = selectedSnapshot("run-2", "New stale node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(screen.queryByText(/Run graph/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Inspector/)).not.toBeInTheDocument();
  });
  it("reloads a surviving selected run once after baseline replacement", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    projectionHarness.selectRun.mockClear();

    projectionHarness.state.runs = { "run-1": summary };
    projectionHarness.state.selectedRunId = undefined;
    projectionHarness.state.selectedRunStatus = "idle";
    projectionHarness.state.selectedRun = undefined;
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    expect(projectionHarness.selectRun).toHaveBeenCalledTimes(1);
    expect(projectionHarness.selectRun).toHaveBeenCalledWith("run-1");

    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    expect(projectionHarness.selectRun).toHaveBeenCalledTimes(1);
  });

  it("falls back to the selected run's workflow when replacement omits its summary", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(screen.getByRole("button", { name: "Run graph Recorded node" }));
    expect(screen.getByText("Inspector run-1")).toBeInTheDocument();
    projectionHarness.selectRun.mockClear();

    projectionHarness.state.runs = {};
    projectionHarness.state.selectedRunId = undefined;
    projectionHarness.state.selectedRunStatus = "idle";
    projectionHarness.state.selectedRun = undefined;
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    expect(projectionHarness.selectRun).toHaveBeenCalledTimes(1);
    expect(projectionHarness.selectRun).toHaveBeenCalledWith(undefined);
    expect(view.container.querySelector(".view-header")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Workflow graph" })).toBeInTheDocument();
    expect(screen.queryByText(/Inspector/)).not.toBeInTheDocument();
    expect(view.container.querySelector(".breadcrumb")).not.toHaveTextContent("run-1");
  });

  it("passes one run-wide live tail to the log pane and restores all-step scope", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    projectionHarness.state.liveLogs = {
      "run-1": [{ sequence: "17" }, { sequence: "18" }],
    };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1/ }));

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    expect(screen.getByText("Live logs 17,18")).toBeInTheDocument();
    expect(screen.getByText("Log scope all")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run graph Recorded node" }));
    expect(screen.getByText("Log scope node-1")).toBeInTheDocument();
    expect(screen.getByText("Inspector run-1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(screen.getByText("Log scope all")).toBeInTheDocument();
    expect(screen.queryByText("Inspector run-1")).not.toBeInTheDocument();
  });

});
