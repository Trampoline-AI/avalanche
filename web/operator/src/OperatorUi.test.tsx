import type * as ReactFlowModule from "@xyflow/react";
import { StrictMode, type ComponentType, type ReactNode, useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// jsdom has no graph viewport. Keep the real cards, controls, inspector, and projection;
// replace only React Flow's layout host, not application components or live state.
vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof ReactFlowModule>()),
  Background: () => null,
  Controls: () => null,
  Handle: () => null,
  Panel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  useViewport: () => ({ x: 0, y: 0, zoom: 1 }),
  useReactFlow: () => ({
    screenToFlowPosition: () => ({ x: 0, y: 0 }),
    setCenter: () => undefined,
  }),
  ReactFlow: ({
    nodes,
    nodeTypes,
    children,
  }: {
    nodes: { id: string; data: unknown; selected?: boolean }[];
    nodeTypes: Record<string, ComponentType<{ data: unknown; selected?: boolean }>>;
    children: ReactNode;
  }) => {
    const Card = nodeTypes.workflow;
    return (
      <div>
        {nodes.map((node) => (
          <Card key={node.id} data={node.data} selected={node.selected} />
        ))}
        {children}
      </div>
    );
  },
}));

import { OperatorUi } from "./index";
import type { OperatorUiSelection } from "./index";
import {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  FlowInfoMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./model";
import {
  baseline,
  createApi,
  envelope,
  eventUlid,
  idleUpdates,
  snapshotFor,
  summary,
  workflow,
} from "./test/fixtures";

const presentation = {
  rootLabel: "Operator",
  brandImageUrl: "/brand.svg",
  unavailableDescription: "Waiting for operator",
  workflowReloadDescription: "Reloading workflows",
};

describe("operator workflows", () => {
  it("selects a workflow, starts without auto-selecting, inspects retained output, and cancels the selected run", async () => {
    const inventory = FlowInfoMsg.create({
      ...workflow,
      workflowId: "inventory.py::inventory",
      displayName: "Inventory",
      relativeFile: "inventory.py",
      displayNames: { fetch: "Inventory fetch" },
    });
    const started = Promise.withResolvers<RunSummaryMsg>();
    const publish = Promise.withResolvers<void>();
    const cancelled = Promise.withResolvers<string>();
    const snapshots = vi.fn(async () =>
      RunSnapshotMsg.create({
        ...snapshotFor(await started.promise),
        asOfEventUlid: eventUlid(2),
        topology: { ...snapshotFor().topology!, displayNames: { fetch: "Recorded fetch" } },
      }),
    );
    const api = createApi({
      loadBaseline: async () => ({
        ...baseline,
        catalog: CatalogSnapshotMsg.create({
          ...baseline.catalog,
          workflows: [inventory, workflow],
        }),
        runs: [],
      }),
      startRun: async (selector) => {
        const run = RunSummaryMsg.create({
          ...summary,
          runId: "run-new",
          workflowId: selector,
        });
        started.resolve(run);
        return run.runId;
      },
      cancelRun: async (runId) => {
        cancelled.resolve(runId);
      },
      getLatestRunSnapshot: snapshots,
      streamUpdates: async function* (_instance, _cursor, signal) {
        const run = await started.promise;
        await publish.promise;
        yield envelope(2, { oneofKind: "runCreated", runCreated: { summary: run, nodes: [] } });
        const runId = await cancelled.promise;
        yield envelope(3, {
          oneofKind: "runStatusChanged",
          runStatusChanged: {
            runId,
            status: "cancelled",
            revision: "2",
            startedAt: 0,
            endedAt: 0,
          },
        });
        yield* idleUpdates(signal);
      },
      listAgentEventPage: async () => ({
        operatorInstanceId: "operator-1",
        asOfEventUlid: eventUlid(2),
        runId: "run-new",
        nodeId: "fetch",
        records: [
          AgentEventDescriptorMsg.create({
            eventSequence: "1",
            eventKind: "run.succeeded",
            bodyToken: "output",
            sizeBytes: "128",
          }),
        ],
        nextPageToken: "",
        nextCursor: "1",
      }),
      readJsonDetail: async () => ({ outputs: { answer: "Retained result" } }),
    });
    render(<OperatorUi host={{ api, presentation }} />);
    await screen.findByRole("button", { name: "Inspect Inventory fetch" });
    fireEvent.click(screen.getByRole("button", { name: /Ordersflows.py/ }));
    await screen.findByRole("button", { name: "Inspect Fetch" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await act(async () => {
      await started.promise;
    });
    expect(screen.queryByRole("button", { name: /run-new,/ })).not.toBeInTheDocument();
    expect(snapshots).not.toHaveBeenCalled();

    act(() => publish.resolve());
    fireEvent.click(await screen.findByRole("button", { name: /run-new, running/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect Recorded fetch" }));
    fireEvent.click(screen.getByRole("button", { name: "output" }));
    expect(await screen.findByText("Retained result")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await screen.findByRole("button", { name: /run-new, cancelled/ });
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Current workflow" }));
    await screen.findByRole("button", { name: "Inspect Fetch" });
    expect(screen.queryByText("Retained result")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Inspect Recorded fetch" }),
    ).not.toBeInTheDocument();
  });

  it("honors a controlled deep link through StrictMode startup and clears inspection when the host navigates away", async () => {
    const ready = Promise.withResolvers<typeof baseline>();
    const api = createApi({ loadBaseline: () => ready.promise });
    function HostedOperator() {
      const [selection, setSelection] = useState<OperatorUiSelection | undefined>({
        kind: "run",
        workflowId: workflow.workflowId,
        runId: summary.runId,
      });
      return (
        <>
          <button
            onClick={() => setSelection({ kind: "workflow", workflowId: workflow.workflowId })}
          >
            Host return to workflow
          </button>
          <OperatorUi
            host={{ api, presentation }}
            navigation={{ selection, onSelectionChange: setSelection }}
          />
        </>
      );
    }
    render(
      <StrictMode>
        <HostedOperator />
      </StrictMode>,
    );
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    act(() => ready.resolve(baseline));
    await screen.findByRole("button", { name: "Cancel run" });
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    expect(screen.getByRole("button", { name: "output" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Host return to workflow" }));
    await screen.findByRole("button", { name: "Run" });
    expect(screen.queryByRole("button", { name: "output" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  });

  it("surfaces operator rejection without losing the selected workflow", async () => {
    const api = createApi({
      startRun: async () => {
        throw new Error("Preparation rejected: missing credential");
      },
    });
    render(<OperatorUi host={{ api, presentation }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(
      await screen.findByText("Preparation rejected: missing credential"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Fetch" })).toBeInTheDocument();
    const runs = screen.getByRole("region", { name: "Workflow runs" });
    await waitFor(() => expect(within(runs).getAllByRole("button")).toHaveLength(1));
  });
});
