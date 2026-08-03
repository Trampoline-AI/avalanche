import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
  },
  selectRun: vi.fn(async (_runId?: string) => undefined),
  startRun: vi.fn(async () => "run-1"),
  cancelRun: vi.fn(async () => undefined),
}));

vi.mock("./GraphCanvas", () => ({
  GraphCanvas: ({
    runTopology,
    onOpenNode,
  }: {
    runTopology?: { displayNames: Record<string, string> };
    onOpenNode: (nodeId: string) => void;
  }) => (
    <button type="button" onClick={() => onOpenNode("node-1")}>
      {runTopology ? `Run graph ${runTopology.displayNames["node-1"]}` : "Workflow graph"}
    </button>
  ),
}));
vi.mock("./Inspector", () => ({
  Inspector: ({
    run,
    liveLogs,
  }: {
    run?: { summary?: { runId: string } };
    liveLogs?: { sequence: string }[];
  }) => (
    <div>
      <span>{`Inspector ${run?.summary?.runId ?? "workflow"}`}</span>
      <span>{`Live logs ${liveLogs?.map((log) => log.sequence).join(",") ?? "none"}`}</span>
    </div>
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
    projectionHarness.state.liveEventRepairWatermarks = {};
    projectionHarness.state.liveLogRepairWatermarks = {};
    projectionHarness.selectRun.mockClear();
    projectionHarness.startRun.mockClear();
    projectionHarness.cancelRun.mockClear();
  });

  it("keeps Explorer available through the compact navigation toggle", () => {
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

  it("navigates summary-only runs with one demand-load selection and clears it", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);

    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 2/ }));

    expect(projectionHarness.selectRun).toHaveBeenCalledTimes(1);
    expect(projectionHarness.selectRun).toHaveBeenCalledWith("run-1");
    expect(screen.getByRole("heading", { name: "No run snapshot" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /demoflow.py/ }));
    expect(projectionHarness.selectRun).toHaveBeenLastCalledWith(undefined);

    fireEvent.click(screen.getByRole("button", { name: /run-1Created at sequence 2/ }));
    view.unmount();
    expect(projectionHarness.selectRun).toHaveBeenLastCalledWith(undefined);
  });

  it("shows selected-run loading and error states without rendering stale snapshots", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 2/ }));

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
    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 2/ }));

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
    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 2/ }));

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
    expect(screen.getByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Workflow graph" })).toBeInTheDocument();
    expect(screen.queryByText(/Inspector/)).not.toBeInTheDocument();
    expect(view.container.querySelector(".breadcrumb")).not.toHaveTextContent("run-1");
  });

  it("passes the selected run and node live-log tail to the inspector", async () => {
    projectionHarness.state.runs = { "run-1": summary };
    projectionHarness.state.liveLogs = {
      "run-1": [{ sequence: "wrong-bucket" }],
      "run-1:node-1": [{ sequence: "17" }, { sequence: "18" }],
    };
    const view = render(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1Created at sequence 2/ }));

    projectionHarness.state.selectedRunId = "run-1";
    projectionHarness.state.selectedRunStatus = "ready";
    projectionHarness.state.selectedRun = selectedSnapshot("run-1", "Recorded node");
    view.rerender(<App api={new GrpcWebOperatorApi("http://localhost")} />);
    fireEvent.click(screen.getByRole("button", { name: "Run graph Recorded node" }));

    expect(screen.getByText("Live logs 17,18")).toBeInTheDocument();
  });

});
