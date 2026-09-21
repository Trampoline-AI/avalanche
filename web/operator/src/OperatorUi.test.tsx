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

import { OperatorUi, WorkflowWorkspace } from "./index";
import type { OperatorUiSelection } from "./index";
import type { OperatorApi } from "./api";
import { Explorer } from "./Explorer";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  LogRecordDescriptorMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
  OperatorUpdateEnvelope,
} from "./model";
import {
  baseline,
  createApi,
  envelope,
  eventUlid,
  idleUpdates,
  snapshotFor,
  secondSummary,
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
  it("starts and selects a run, inspects its node, and cancels it", async () => {
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
      getWorkflowNodeSource: async () => "def fetch():\n    return 'Current source'",
    });
    render(<OperatorUi host={{ api, presentation }} />);
    await screen.findByRole("button", { name: "Inspect Inventory fetch" });
    fireEvent.click(screen.getByRole("button", { name: /Orders\s*flows\.py/ }));
    await screen.findByRole("button", { name: "Inspect Fetch" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await act(async () => {
      await started.promise;
    });
    expect(screen.queryByRole("button", { name: /run-new,/ })).not.toBeInTheDocument();
    await screen.findByRole("button", { name: "Inspect Recorded fetch" });

    act(() => publish.resolve());
    await screen.findByRole("button", { name: /run-new, running/ });
    fireEvent.click(await screen.findByRole("button", { name: "Inspect Recorded fetch" }));
    expect(screen.getByRole("region", { name: "Step interface" })).toHaveTextContent(
      /unavailable/i,
    );
    expect(screen.queryByRole("tab", { name: "Run I/O" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await screen.findByRole("button", { name: /run-new, cancelled/ });
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    await screen.findByRole("button", { name: "Inspect Fetch" });
    expect(screen.getByRole("tab", { name: "Definition" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Inspect Recorded fetch" }),
    ).not.toBeInTheDocument();
  });

  it.each(["operator", "workspace"] as const)(
    "%s navigation returns to the current workflow and retains the selected node",
    async (host) => {
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
            {host === "operator" ? (
              <OperatorUi
                host={{ api, presentation }}
                navigation={{ selection, onSelectionChange: setSelection }}
              />
            ) : (
              <WorkflowWorkspace
                api={api}
                workflowId={workflow.workflowId}
                navigation={{
                  selectedRunId: selection?.kind === "run" ? selection.runId : undefined,
                  onSelectRun: (runId) =>
                    setSelection(
                      runId === undefined
                        ? { kind: "workflow", workflowId: workflow.workflowId }
                        : { kind: "run", workflowId: workflow.workflowId, runId },
                    ),
                }}
              />
            )}
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
      expect(screen.getByRole("region", { name: "Step interface" })).toHaveTextContent(
        /unavailable/i,
      );
      expect(screen.getByText("Viewing a run snapshot")).toBeInTheDocument();
      expect(
        screen.getByText("This view does not represent the workflow's current state."),
      ).toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", {
          name: host === "operator" ? "View current state for Orders" : "Current",
        }),
      );
      await screen.findByRole("button", { name: "Run" });
      expect(screen.getByRole("tab", { name: "Definition" })).toBeVisible();
      expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    },
  );

  it("keeps a collapsed rail available and pins a hovered Explorer", async () => {
    render(<OperatorUi host={{ api: createApi(), presentation }} />);

    const collapse = await screen.findByRole("button", { name: "Collapse Explorer" });
    const explorer = screen.getByRole("complementary", { name: "Explorer" });
    fireEvent.click(collapse);

    expect(
      screen.queryByRole("button", { name: /Orders\s*flows\.py/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pin Explorer open" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    fireEvent.mouseEnter(explorer);
    fireEvent.click(screen.getByRole("button", { name: /Orders\s*flows\.py/ }));
    fireEvent.mouseLeave(explorer);
    expect(
      screen.queryByRole("button", { name: /Orders\s*flows\.py/ }),
    ).not.toBeInTheDocument();

    fireEvent.mouseEnter(explorer);
    fireEvent.click(screen.getByRole("button", { name: "Pin Explorer open" }));
    fireEvent.mouseLeave(explorer);
    expect(screen.getByRole("button", { name: /Orders\s*flows\.py/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse Explorer" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("groups only an unambiguous scan target and keeps all workflows when targets change", () => {
    const target = {
      alias: "examples",
      targetPath: "/workspace/examples/flows.py",
      kind: "file",
    };
    const workflows = [
      FlowInfoMsg.create({ ...workflow, rootAlias: target.alias }),
      FlowInfoMsg.create({
        workflowId: "inventory.py::inventory",
        displayName: "Inventory",
        relativeFile: "inventory.py",
        rootAlias: "another-root",
      }),
    ];
    const props = {
      onSelect: vi.fn(),
      selection: { kind: "run", workflowId: workflow.workflowId, runId: "run-1" } as const,
    };
    const { rerender } = render(
      <Explorer
        {...props}
        catalog={CatalogSnapshotMsg.create({ workflows, scanTargets: [target] })}
      />,
    );
    const group = screen.getByRole("region", { name: "flows.py" });
    expect(within(group).getByRole("button", { name: /Orders\s*flows\.py/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(
      within(group).getByRole("button", { name: /Inventory\s*inventory\.py/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(target.targetPath)).not.toBeInTheDocument();

    rerender(
      <Explorer
        {...props}
        catalog={CatalogSnapshotMsg.create({
          workflows,
          scanTargets: [
            target,
            { alias: "other", targetPath: "/workspace/other", kind: "directory" },
          ],
        })}
      />,
    );
    expect(screen.queryByRole("region", { name: "flows.py" })).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Workflows" })).getAllByRole("button"),
    ).toHaveLength(2);

    rerender(<Explorer {...props} catalog={CatalogSnapshotMsg.create({ workflows })} />);
    expect(
      within(screen.getByRole("region", { name: "Workflows" })).getAllByRole("button"),
    ).toHaveLength(2);
  });

  it("does not return to a workflow when its start request finishes after navigation", async () => {
    const inventory = FlowInfoMsg.create({
      ...workflow,
      workflowId: "inventory.py::inventory",
      displayName: "Inventory",
      relativeFile: "inventory.py",
      displayNames: { fetch: "Inventory fetch" },
    });
    const started = Promise.withResolvers<string>();
    const api = createApi({
      loadBaseline: async () => ({
        ...baseline,
        catalog: CatalogSnapshotMsg.create({
          ...baseline.catalog,
          workflows: [workflow, inventory],
        }),
      }),
      startRun: () => started.promise,
      getLatestRunSnapshot: async () =>
        snapshotFor(RunSummaryMsg.create({ ...summary, runId: "run-new" })),
    });
    render(<OperatorUi host={{ api, presentation }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    fireEvent.click(screen.getByRole("button", { name: /Inventory\s*inventory\.py/ }));
    await screen.findByRole("button", { name: "Inspect Inventory fetch" });
    await act(async () => started.resolve("run-new"));
    expect(screen.getByRole("button", { name: "Inspect Inventory fetch" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("region", { name: "Run logs" })).not.toBeInTheDocument();
  });

  it.each(["workflow", "run", "pending host navigation", "round trip", "unmount"] as const)(
    "does not select a started run after controlled %s navigation",
    async (destination) => {
      const inventory = FlowInfoMsg.create({
        ...workflow,
        workflowId: "inventory.py::inventory",
        displayNames: { fetch: "Inventory fetch" },
      });
      const started = Promise.withResolvers<string>();
      const onSelectRun = vi.fn();
      const api = createApi({
        loadBaseline: async () => ({
          ...baseline,
          catalog: CatalogSnapshotMsg.create({
            ...baseline.catalog,
            workflows: [workflow, inventory],
          }),
        }),
        startRun: () => started.promise,
      });
      const workspace = (workflowId: string, selectedRunId?: string) => (
        <WorkflowWorkspace
          api={api}
          workflowId={workflowId}
          navigation={{ selectedRunId, onSelectRun }}
        />
      );
      const view = render(workspace(workflow.workflowId));
      fireEvent.click(await screen.findByRole("button", { name: "Run" }));
      if (destination === "unmount") {
        view.unmount();
      } else if (destination === "pending host navigation") {
        fireEvent.click(screen.getByRole("button", { name: /run-1,/ }));
      } else if (destination === "run") {
        view.rerender(workspace(workflow.workflowId, summary.runId));
        await screen.findByRole("button", { name: "Cancel run" });
      } else {
        view.rerender(workspace(inventory.workflowId));
        await screen.findByRole("button", { name: "Inspect Inventory fetch" });
        if (destination === "round trip") {
          view.rerender(workspace(workflow.workflowId));
          await screen.findByRole("button", { name: "Inspect Fetch" });
        }
      }
      await act(async () => started.resolve("run-new"));
      if (destination === "pending host navigation") {
        expect(onSelectRun).toHaveBeenCalledExactlyOnceWith(summary.runId);
      } else {
        expect(onSelectRun).not.toHaveBeenCalled();
      }
      if (destination === "run") {
        expect(screen.getByRole("button", { name: /run-1,/ })).toHaveAttribute(
          "aria-pressed",
          "true",
        );
      } else if (destination !== "unmount") {
        expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
          "aria-pressed",
          "true",
        );
      }
    },
  );

  it.each(["operator", "workspace"] as const)(
    "%s disables cancellation until the pending request settles",
    async (host) => {
      const cancelled = Promise.withResolvers<void>();
      const cancelRun = vi.fn(() => cancelled.promise);
      const api = createApi({ cancelRun });
      render(
        host === "operator" ? (
          <OperatorUi host={{ api, presentation }} />
        ) : (
          <WorkflowWorkspace api={api} workflowId={workflow.workflowId} />
        ),
      );
      fireEvent.click(await screen.findByRole("button", { name: /run-1,/ }));
      fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
      const pending = screen.getByRole("button", { name: "Cancelling…" });
      expect(pending).toBeDisabled();
      fireEvent.click(pending);
      expect(cancelRun).toHaveBeenCalledTimes(1);
      await act(async () => cancelled.resolve());
      expect(screen.getByRole("button", { name: "Cancel run" })).toBeEnabled();
    },
  );

  it("keeps historical workspace content and navigation after its definition is removed", async () => {
    const removed = Promise.withResolvers<void>();
    const nextSnapshot = Promise.withResolvers<RunSnapshotMsg>();
    const third = RunSummaryMsg.create({ ...summary, runId: "run-3", createdSequence: "3" });
    const run = RunSnapshotMsg.create({
      ...snapshotFor(),
      logPageToken: "logs",
      topology: {
        ...snapshotFor().topology!,
        displayNames: { fetch: "Recorded fetch" },
        agentFieldSchemasJson: { fetch: '{"inputs":[],"outputs":[]}' },
      },
    });
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs: [summary, secondSummary] }),
      getLatestRunSnapshot: async (runId) =>
        runId === secondSummary.runId ? nextSnapshot.promise : run,
      listLogPage: async () => ({
        operatorInstanceId: "operator-1",
        asOfEventUlid: eventUlid(1),
        records: [
          LogRecordDescriptorMsg.create({
            sequence: "1",
            timestamp: 1,
            nodeId: "fetch",
            bodyToken: "body",
            level: "info",
          }),
        ],
        nextPageToken: "",
        nextCursor: "1",
      }),
      readTextDetail: async () => "Recorded order log",
      streamUpdates: async function* (_instance, _cursor, signal) {
        await removed.promise;
        yield envelope(2, {
          oneofKind: "catalogReplaced",
          catalogReplaced: {
            catalog: CatalogSnapshotMsg.create({
              ...baseline.catalog,
              asOfEventUlid: eventUlid(2),
              revision: "2",
              workflows: [],
            }),
          },
        });
        yield envelope(3, {
          oneofKind: "runCreated",
          runCreated: { summary: third, nodes: [] },
        });
        yield* idleUpdates(signal);
      },
    });
    render(<WorkflowWorkspace api={api} workflowId={workflow.workflowId} />);
    fireEvent.click(await screen.findByRole("button", { name: /run-1,/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect Recorded fetch" }));
    await screen.findByText("Recorded order log");
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    act(() => removed.resolve());
    await screen.findByRole("button", { name: /run-3,/ });
    expect(screen.getByRole("button", { name: /run-1,/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Inspect Recorded fetch" })).toBeEnabled();
    expect(screen.getByText("Recorded order log")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /run-2,/ }));
    await screen.findByText("Loading run snapshot");
    expect(screen.getByRole("button", { name: "Inspect Recorded fetch" })).toBeEnabled();
    expect(screen.getByText("Recorded order log")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    act(() =>
      nextSnapshot.resolve(
        RunSnapshotMsg.create({
          ...run,
          asOfEventUlid: eventUlid(3),
          summary: secondSummary,
          topology: { ...run.topology!, displayNames: { fetch: "Next recorded fetch" } },
        }),
      ),
    );
    await screen.findByRole("button", { name: "Inspect Next recorded fetch" });
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    expect(screen.getByText("No workflows discovered")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Run logs" })).not.toBeInTheDocument();
  });

  it("surfaces operator rejection without losing the selected workflow", async () => {
    const api = createApi({
      startRun: async () => {
        throw new Error("Preparation rejected: missing credential");
      },
    });
    render(<OperatorUi host={{ api, presentation }} />);
    await screen.findByRole("button", { name: "Current" });
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(
      await screen.findByText("Preparation rejected: missing credential"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Fetch" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe.each(["hosted", "local"] as const)("%s shared workspace", (host) => {
  function mount(api: OperatorApi) {
    return render(
      <StrictMode>
        {host === "hosted" ? (
          <WorkflowWorkspace
            api={api}
            workflowId={workflow.workflowId}
            runActionsEnabled={false}
          />
        ) : (
          <OperatorUi host={{ api, presentation }} />
        )}
      </StrictMode>,
    );
  }

  it("follows an explicitly selected latest run through bursts and baselines, but keeps an older run pinned", async () => {
    const ready = Promise.withResolvers<void>();
    const thirdArrives = Promise.withResolvers<void>();
    const fourthArrives = Promise.withResolvers<void>();
    const reset = Promise.withResolvers<void>();
    const sixthArrives = Promise.withResolvers<void>();
    const staleSnapshot = Promise.withResolvers<RunSnapshotMsg>();
    const third = RunSummaryMsg.create({ ...summary, runId: "run-3", createdSequence: "3" });
    const fourth = RunSummaryMsg.create({ ...summary, runId: "run-4", createdSequence: "4" });
    const fifth = RunSummaryMsg.create({ ...summary, runId: "run-5", createdSequence: "5" });
    const sixth = RunSummaryMsg.create({ ...summary, runId: "run-6", createdSequence: "6" });
    let runs = [
      summary,
      secondSummary,
      RunSummaryMsg.create({
        ...summary,
        runId: "another-workflow-run",
        workflowId: "another-workflow",
        createdSequence: "100",
      }),
    ];
    let cursor = 1;
    let connections = 0;
    const snapshot = (run: RunSummaryMsg) =>
      RunSnapshotMsg.create({
        ...snapshotFor(run),
        asOfEventUlid: eventUlid(cursor),
        topology: {
          ...snapshotFor(run).topology!,
          displayNames: { fetch: `Snapshot ${run.runId}` },
        },
      });
    const api = createApi({
      loadBaseline: async () => {
        await ready.promise;
        return { ...baseline, asOfEventUlid: eventUlid(cursor), runs };
      },
      getLatestRunSnapshot: async (runId) =>
        runId === secondSummary.runId
          ? staleSnapshot.promise
          : snapshot(runs.find((run) => run.runId === runId)!),
      streamUpdates: async function* (_instance, _cursor, signal) {
        connections += 1;
        if (connections === 1) {
          await thirdArrives.promise;
          yield envelope(2, {
            oneofKind: "runCreated",
            runCreated: { summary: third, nodes: [] },
          });
          await fourthArrives.promise;
          yield envelope(3, {
            oneofKind: "runCreated",
            runCreated: { summary: fourth, nodes: [] },
          });
          await reset.promise;
          yield OperatorUpdateEnvelope.create({
            operatorInstanceId: "operator-1",
            payload: {
              oneofKind: "resetRequired",
              resetRequired: {
                latestEventUlid: eventUlid(4),
                historyFloorEventUlid: eventUlid(4),
              },
            },
          });
          return;
        }
        await sixthArrives.promise;
        yield envelope(5, {
          oneofKind: "runCreated",
          runCreated: { summary: sixth, nodes: [] },
        });
        yield* idleUpdates(signal);
      },
    });
    mount(api);
    act(() => ready.resolve());
    fireEvent.click(await screen.findByRole("button", { name: /run-2,/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /run-2,/ })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    act(() => {
      runs = [...runs, third];
      cursor = 2;
      thirdArrives.resolve();
    });
    await screen.findByRole("button", { name: "Inspect Snapshot run-3" });
    act(() => staleSnapshot.resolve(snapshot(secondSummary)));
    expect(screen.getByRole("button", { name: /run-3,/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /run-1,/ }));
    await screen.findByRole("button", { name: "Inspect Snapshot run-1" });
    act(() => {
      runs = [...runs, fourth];
      cursor = 3;
      fourthArrives.resolve();
    });
    await screen.findByRole("button", { name: /run-4,/ });
    expect(screen.getByRole("button", { name: /run-1,/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    act(() => {
      runs = [...runs, fifth];
      cursor = 4;
      reset.resolve();
    });
    await screen.findByRole("button", { name: /run-5,/ });
    await screen.findByRole("button", { name: "Inspect Snapshot run-1" });
    expect(screen.getByRole("button", { name: /run-1,/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /run-5,/ }));
    await screen.findByRole("button", { name: "Inspect Snapshot run-5" });
    act(() => {
      runs = [...runs, sixth];
      cursor = 5;
      sixthArrives.resolve();
    });
    await screen.findByRole("button", { name: "Inspect Snapshot run-6" });
    expect(screen.getByRole("button", { name: /run-6,/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("opens static nodes without selecting a run or loading runtime data", async () => {
    const snapshots = vi.fn();
    const logs = vi.fn();
    mount(
      createApi({
        getLatestRunSnapshot: snapshots,
        listLogPage: logs,
      }),
    );
    const node = await screen.findByRole("button", { name: "Inspect Fetch" });
    expect(node).toBeEnabled();
    expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(node);
    expect(screen.getByRole("tab", { name: "Definition" })).toBeVisible();
    expect(snapshots).not.toHaveBeenCalled();
    expect(logs).not.toHaveBeenCalled();
  });

  it("clears loading and failed runs without late snapshots or reconnect restoring selection", async () => {
    const lateSnapshot = Promise.withResolvers<RunSnapshotMsg>();
    const reset = Promise.withResolvers<void>();
    const third = RunSummaryMsg.create({ ...summary, runId: "run-3", createdSequence: "3" });
    let connections = 0;
    let snapshotRequests = 0;
    let runs = [summary, secondSummary];
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs }),
      getLatestRunSnapshot: async () => {
        snapshotRequests += 1;
        if (snapshotRequests === 1) return lateSnapshot.promise;
        throw new Error("Snapshot was removed");
      },
      streamUpdates: async function* (_instance, _cursor, signal) {
        connections += 1;
        if (connections === 1) {
          await reset.promise;
          yield OperatorUpdateEnvelope.create({
            operatorInstanceId: "operator-1",
            payload: {
              oneofKind: "resetRequired",
              resetRequired: {
                latestEventUlid: eventUlid(1),
                historyFloorEventUlid: eventUlid(1),
              },
            },
          });
          return;
        }
        yield* idleUpdates(signal);
      },
    });
    mount(api);
    fireEvent.click(await screen.findByRole("button", { name: "Inspect Fetch" }));
    fireEvent.click(screen.getByRole("button", { name: /run-2,/ }));
    await screen.findByText("Loading run snapshot");
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    expect(screen.getByRole("tab", { name: "Definition" })).toBeVisible();
    await act(async () => lateSnapshot.resolve(snapshotFor(secondSummary)));
    expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("region", { name: "Run logs" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /run-2,/ }));
    await screen.findByText("Snapshot was removed");
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    expect(screen.getByRole("tab", { name: "Definition" })).toBeVisible();
    act(() => {
      runs = [...runs, third];
      reset.resolve();
    });
    await screen.findByRole("button", { name: /run-3,/ });
    expect(screen.getByRole("button", { name: "Current" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("region", { name: "Run logs" })).not.toBeInTheDocument();
    expect(snapshotRequests).toBe(2);
  });

  it("opens retained step interfaces and agent traces without loading current source in run view", async () => {
    const getWorkflowNodeSource = vi.fn(
      async () => "def fetch():\n    return 'current source'",
    );
    const currentWorkflow = FlowInfoMsg.create({
      ...workflow,
      stepInterfaceJson: {
        fetch: JSON.stringify({
          step_inputs: [
            { name: "limit", type_name: "int", required: false, json_schema: null },
          ],
          step_output: { type_name: "CurrentBatch", json_schema: null },
        }),
      },
    });
    const run = RunSnapshotMsg.create({
      ...snapshotFor(),
      topology: {
        ...snapshotFor().topology!,
        nodeIds: ["fetch", "review"],
        graph: { fetch: { children: ["review"] }, review: { children: [] } },
        nodeTypes: { fetch: "step", review: "step" },
        displayNames: { fetch: "Fetch", review: "Review" },
        agentFieldSchemasJson: { review: '{"inputs":[],"outputs":[]}' },
        stepInterfaceJson: {
          fetch: JSON.stringify({
            step_inputs: [],
            step_output: { type_name: "HistoricalBatch", json_schema: null },
          }),
        },
      },
    });
    mount(
      createApi({
        loadBaseline: async () => ({
          ...baseline,
          catalog: CatalogSnapshotMsg.create({
            ...baseline.catalog,
            workflows: [currentWorkflow],
          }),
        }),
        getLatestRunSnapshot: async () => run,
        getWorkflowNodeSource,
      }),
    );
    const currentCard = (await screen.findByRole("button", { name: "Inspect Fetch" })).closest(
      "article",
    )!;
    expect(within(currentCard).getByRole("region", { name: "Inputs" })).toHaveTextContent(
      "limit",
    );
    expect(within(currentCard).getByRole("region", { name: "Outputs" })).toHaveTextContent(
      "CurrentBatch",
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    expect(getWorkflowNodeSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(await screen.findByLabelText("Source code")).toHaveTextContent("current source");
    getWorkflowNodeSource.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /run-1,/ }));
    await screen.findByRole("button", { name: "Inspect Review" });
    expect(screen.queryByRole("tab", { name: "Code" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    const historicalCard = screen
      .getByRole("button", { name: "Inspect Fetch" })
      .closest("article")!;
    expect(within(historicalCard).getByRole("region", { name: "Outputs" })).toHaveTextContent(
      "HistoricalBatch",
    );
    expect(historicalCard).not.toHaveTextContent("CurrentBatch");
    expect(screen.getByRole("region", { name: "Step output" })).toHaveTextContent(
      "HistoricalBatch",
    );
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    expect(getWorkflowNodeSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Inspect Review" }));
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Trace" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    expect(screen.getByRole("region", { name: "Step output" })).toHaveTextContent(
      "HistoricalBatch",
    );
    expect(getWorkflowNodeSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    expect(screen.getByRole("tab", { name: "Definition" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Code" }));
    expect(await screen.findByLabelText("Source code")).toHaveTextContent("current source");
  });

  it("retains a selected node while loading another topology and clears it only when absent", async () => {
    const firstSnapshot = Promise.withResolvers<RunSnapshotMsg>();
    const nextSnapshot = Promise.withResolvers<RunSnapshotMsg>();
    const third = RunSummaryMsg.create({ ...summary, runId: "run-3", createdSequence: "3" });
    const agentSnapshot = RunSnapshotMsg.create({
      ...snapshotFor(),
      topology: {
        ...snapshotFor().topology!,
        agentFieldSchemasJson: { fetch: '{"inputs":[],"outputs":[]}' },
      },
    });
    mount(
      createApi({
        loadBaseline: async () => ({ ...baseline, runs: [summary, secondSummary, third] }),
        getLatestRunSnapshot: async (runId) => {
          if (runId === summary.runId) return firstSnapshot.promise;
          if (runId === secondSummary.runId) return nextSnapshot.promise;
          if (runId === third.runId) {
            return RunSnapshotMsg.create({
              ...snapshotFor(third),
              topology: {
                ...snapshotFor(third).topology!,
                nodeIds: ["replacement"],
                graph: { replacement: { children: [] } },
                nodeTypes: { replacement: "step" },
                displayNames: { replacement: "Replacement" },
              },
              nodes: [],
            });
          }
          return snapshotFor();
        },
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Inspect Fetch" }));
    fireEvent.click(screen.getByRole("button", { name: /run-1,/ }));
    await screen.findByText("Loading run snapshot");
    expect(screen.queryByLabelText("Source code")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Fetch" })).toBeEnabled();
    act(() => firstSnapshot.resolve(agentSnapshot));
    await screen.findByRole("complementary", { name: "Run inspector" });
    fireEvent.click(screen.getByRole("button", { name: /run-2,/ }));
    await screen.findByText("Loading run snapshot");
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect Fetch" })).toBeEnabled();
    act(() =>
      nextSnapshot.resolve(
        RunSnapshotMsg.create({
          ...agentSnapshot,
          summary: secondSummary,
          topology: { ...agentSnapshot.topology!, displayNames: { fetch: "Renamed fetch" } },
        }),
      ),
    );
    await screen.findByRole("button", { name: "Inspect Renamed fetch" });
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /run-3,/ }));
    await screen.findByRole("button", { name: "Inspect Replacement" });
    expect(
      screen.queryByRole("complementary", { name: "Run inspector" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    expect(screen.queryByRole("tab", { name: "Definition" })).not.toBeInTheDocument();
  });

  it("expands the timeline independently of node inspection and retains filters on collapse", async () => {
    mount(
      createApi({
        loadBaseline: async () => ({
          ...baseline,
          runs: [
            RunSummaryMsg.create({
              ...summary,
              triggeredAt: new Date(2026, 8, 9, 12).getTime() / 1000,
            }),
            RunSummaryMsg.create({
              ...secondSummary,
              triggeredAt: new Date(2026, 8, 10, 12).getTime() / 1000,
            }),
          ],
        }),
        getLatestRunSnapshot: async (runId) =>
          RunSnapshotMsg.create({
            ...snapshotFor(runId === secondSummary.runId ? secondSummary : summary),
            topology: {
              ...snapshotFor().topology!,
              agentFieldSchemasJson: { fetch: '{"inputs":[],"outputs":[]}' },
            },
          }),
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /run-2,/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Inspect Fetch" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand timeline" }));
    const allRuns = screen.getByRole("region", { name: "Timeline" });
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    expect(within(allRuns).getByRole("button", { name: "Collapse timeline" })).toHaveFocus();
    fireEvent.click(within(allRuns).getByText("Advanced filters"));
    fireEvent.change(within(allRuns).getByRole("searchbox", { name: "Run ID" }), {
      target: { value: "run-1" },
    });
    await within(allRuns).findByRole("button", { name: /run-1,/ });
    expect(within(allRuns).queryByRole("button", { name: /run-2,/ })).not.toBeInTheDocument();
    fireEvent.change(within(allRuns).getByRole("searchbox"), { target: { value: "" } });
    fireEvent.change(within(allRuns).getByLabelText("From"), {
      target: { value: "2026-09-10" },
    });
    fireEvent.change(within(allRuns).getByLabelText("To"), { target: { value: "2026-09-10" } });
    await within(allRuns).findByRole("button", { name: /run-2,/ });
    expect(within(allRuns).queryByRole("button", { name: /run-1,/ })).not.toBeInTheDocument();
    fireEvent.keyDown(within(allRuns).getByRole("searchbox"), { key: "Escape" });
    expect(within(allRuns).queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand timeline" })).toHaveFocus();
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand timeline" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect Fetch" }));
    expect(within(allRuns).getByLabelText("From")).toHaveValue("2026-09-10");
    expect(within(allRuns).getByLabelText("To")).toHaveValue("2026-09-10");
    expect(within(allRuns).queryByRole("button", { name: /run-1,/ })).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Run inspector" })).toBeInTheDocument();
    fireEvent.click(within(allRuns).getByRole("button", { name: "Collapse timeline" }));
    expect(screen.getByRole("region", { name: "Timeline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand timeline" })).toHaveFocus();
  });
});
