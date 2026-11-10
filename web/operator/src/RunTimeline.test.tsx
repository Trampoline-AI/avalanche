import type * as ReactFlowModule from "@xyflow/react";
import type { ComponentType, ReactNode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { OperatorApi } from "./api";
import type {
  MachineInstance,
  MachineSpec,
  MachineTransition,
  MachineVisit,
  MachineVisitEvent,
} from "./machine";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  NodeSnapshotMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
  WorkflowTopologyMsg,
} from "./model";
import {
  baseline,
  createApi,
  eventUlid,
  snapshotFor,
  summary,
  workflow,
} from "./test/fixtures";
import type { WaitCard } from "./RunWaitPanel";
import { WorkflowWorkspace } from "./WorkflowWorkspace";

// jsdom has no graph viewport: keep the real cards and node classes, drop React
// Flow's layout host (as in OperatorUi.test.tsx).
vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof ReactFlowModule>()),
  Background: () => null,
  Controls: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Handle: () => null,
  Panel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  useViewport: () => ({ x: 0, y: 0, zoom: 1 }),
  useStore: (selector: (state: { width: number }) => unknown) => selector({ width: 0 }),
  useUpdateNodeInternals: () => () => undefined,
  useReactFlow: () => ({
    viewportInitialized: false,
    screenToFlowPosition: () => ({ x: 0, y: 0 }),
    fitView: () => undefined,
    setCenter: () => undefined,
  }),
  ReactFlow: ({
    nodes,
    nodeTypes,
    children,
  }: {
    nodes: {
      id: string;
      type: string;
      data: unknown;
      className?: string;
      selected?: boolean;
    }[];
    nodeTypes: Record<string, ComponentType<{ data: unknown }>>;
    children: ReactNode;
  }) => (
    <div>
      {nodes.map((node) => {
        const Card = nodeTypes[node.type];
        return (
          <div
            key={node.id}
            data-id={node.id}
            data-selected={node.selected || undefined}
            className={`react-flow__node ${node.className ?? ""}`}
          >
            <Card data={node.data} />
          </div>
        );
      })}
      {children}
    </div>
  ),
}));

const T0 = 1_800_000_000;
const spec: MachineSpec = {
  schema_version: "avalanche.machine/v1",
  name: "pr_lifecycle",
  input_schema: { type: "object" },
  key_fields: ["repo", "number"],
  initial: "in_review",
  states: {
    in_review: {
      work: "review_round",
      initial: true,
      final: false,
      defer: [],
      timers: [],
      transitions: [{ event: "review_done_with_nits", target: "amend", payload_schema: null }],
    },
    amend: {
      work: "amend_round",
      initial: false,
      final: false,
      defer: ["merged"],
      timers: [],
      transitions: [{ event: "amended", target: "ready", payload_schema: null }],
    },
    ready: {
      work: "notify_ready",
      initial: false,
      final: false,
      defer: [],
      timers: [{ delay_seconds: 600, target: "closed" }],
      transitions: [{ event: "merged", target: "closed", payload_schema: null }],
    },
    closed: {
      work: "cleanup",
      initial: false,
      final: true,
      defer: [],
      timers: [],
      transitions: [],
    },
  },
  work_graphs: {},
};
const machineFlow = FlowInfoMsg.create({
  name: "pr_lifecycle",
  displayName: "pr_lifecycle",
  workflowId: "flows.py::pr_lifecycle",
  relativeFile: "flows.py",
  machineSpec: spec,
});
const machineRun = RunSummaryMsg.create({
  runId: "run-m",
  workflowId: machineFlow.workflowId,
  workflowDisplayName: "pr_lifecycle",
  status: "waiting",
  label: "acme/api #43",
  createdSequence: "3",
  revision: "5",
  triggeredAt: T0,
});
const history: MachineTransition[] = [
  {
    from: "in_review",
    to: "amend",
    event: "review_done_with_nits",
    source: "internal",
    visit: 1,
  },
  { from: "amend", to: "ready", event: "amended", source: "external", visit: 2 },
];
const visitRecord = (visit: number, state: string, entered: number, left: number | null) =>
  ({
    visit,
    state,
    entered_at: entered,
    left_at: left,
    disposition: left === null ? "current" : "retained",
    transition: history.find((item) => item.visit === visit) ?? null,
    nodes: {},
  }) satisfies MachineVisit;
const facts: Record<number, MachineVisitEvent[]> = {
  1: [],
  2: [
    {
      tseq: 1,
      at: T0 + 25,
      status: "accepted",
      event: "merged",
      receipt_id: "r-1",
      source: "external",
    },
    {
      tseq: 2,
      at: T0 + 26,
      status: "accepted",
      event: "amended",
      receipt_id: "r-2",
      source: "external",
    },
  ],
  3: [],
};

function machineApi(overrides: Partial<OperatorApi> = {}, foreign = false) {
  const instance: MachineInstance = {
    machine: "pr_lifecycle",
    key: ["acme/api", 43],
    run_id: "run-m",
    deployment_id: "",
    status: "waiting",
    state: "ready",
    visit: 3,
    version: 1,
    accepts: ["merged"],
    due: T0 + 630,
    finished: false,
    ...(foreign
      ? {}
      : {
          pending_events: [],
          receipts: [
            {
              receipt_id: "r-1",
              key: "k-merged",
              accepted_at: T0 + 25,
              status: "accepted",
              duplicate: false,
            },
            {
              receipt_id: "r-2",
              key: "k-amended",
              accepted_at: T0 + 26,
              status: "accepted",
              duplicate: false,
            },
          ],
        }),
  };
  const visits: Record<number, MachineVisit> = {
    1: visitRecord(1, "in_review", T0 + 1, T0 + 20),
    2: visitRecord(2, "amend", T0 + 20, T0 + 30),
    3: visitRecord(3, "ready", T0 + 30, null),
  };
  const api = createApi({
    loadBaseline: async () => ({
      ...baseline,
      catalog: CatalogSnapshotMsg.create({
        ...baseline.catalog,
        workflows: [workflow, machineFlow],
      }),
      runs: [summary, machineRun],
    }),
    getLatestRunSnapshot: async (runId) =>
      runId === "run-m"
        ? RunSnapshotMsg.create({
            operatorInstanceId: "operator-1",
            asOfEventUlid: eventUlid(1),
            summary: machineRun,
            topology: WorkflowTopologyMsg.create({}),
          })
        : snapshotFor(),
    getMachineInstance: async () => instance,
    getMachineHistory: async () => ({ items: history, next_cursor: null }),
    getMachineVisit: async (_runId, visit) => visits[visit],
    ...(foreign
      ? {}
      : {
          getMachineVisitEvents: async (_runId: string, visit: number) => ({
            items: facts[visit] ?? [],
            next_cursor: null,
          }),
        }),
    ...overrides,
  });
  return api;
}

function renderRun(api: OperatorApi, workflowId: string, runId: string) {
  return render(
    <WorkflowWorkspace
      api={api}
      workflowId={workflowId}
      layoutStorageKey={`timeline-test-${Math.random()}`}
      navigation={{ selectedRunId: runId, onSelectRun: vi.fn() }}
    />,
  );
}

const cardAction = (container: HTMLElement, id: string) =>
  container.querySelector<HTMLButtonElement>(
    `.react-flow__node[data-id="${id}"] .node-card-action`,
  )!;

const events = () =>
  within(screen.getByRole("list", { name: "Run events" }))
    .getAllByRole("listitem")
    .map((row) => row.getAttribute("data-entry-id"));

describe("run timeline in the bottom pane", () => {
  it("replaces the Visits and Waits sections of the runs pane with a Timeline tab", async () => {
    renderRun(machineApi(), machineFlow.workflowId, "run-m");
    const tab = await screen.findByRole("tab", { name: "Timeline" });
    expect(screen.getByRole("tab", { name: /^Logs/ })).toBeInTheDocument();
    // A machine run opens on its timeline.
    expect(tab).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(events()).toContain("v2:fact:1"));
    const runs = screen.getByRole("region", { name: "Runs" });
    expect(within(runs).queryByText("Visits")).not.toBeInTheDocument();
    expect(within(runs).queryByText("Waits")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Machine visits" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Run waits section")).not.toBeInTheDocument();
    expect(events()).toEqual([
      "run-started",
      "v1:enter",
      "v1:leave",
      "v2:enter",
      "v2:fact:1",
      "v2:fact:2",
      "v2:leave",
      "v3:enter",
      "v3:timer:0:armed",
    ]);
    const timeline = screen.getByRole("region", { name: "Run timeline" });
    expect(within(timeline).getByText("Event merged deferred")).toBeInTheDocument();
    expect(within(timeline).getByText(/receipt r-2 · key k-amended/)).toBeInTheDocument();
    // What the run waits for now stays pinned on top.
    const pending = within(timeline).getByRole("list", { name: "Pending" });
    expect(within(pending).getByText("Timer → closed")).toBeInTheDocument();
    expect(pending.querySelector("[data-timer-remaining]")).not.toBeNull();
    expect(within(pending).getByText("Waiting for event")).toBeInTheDocument();
  });

  it("highlights an entry's state on the graph and filters to a state picked on the graph", async () => {
    const { container } = renderRun(machineApi(), machineFlow.workflowId, "run-m");
    await waitFor(() => expect(events()).toContain("v2:enter"));
    const amendCard = () => container.querySelector('.react-flow__node[data-id="amend"]')!;
    expect(amendCard()).not.toHaveClass("timeline-highlight");
    const row = container.querySelector('[data-entry-id="v2:enter"]')!;
    fireEvent.mouseEnter(row);
    await waitFor(() => expect(amendCard()).toHaveClass("timeline-highlight"));
    fireEvent.mouseLeave(screen.getByRole("region", { name: "Run timeline" }));
    await waitFor(() => expect(amendCard()).not.toHaveClass("timeline-highlight"));
    // Selecting keeps the highlight after the pointer leaves.
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /Entered amend/ }));
    fireEvent.mouseLeave(screen.getByRole("region", { name: "Run timeline" }));
    expect(amendCard()).toHaveClass("timeline-highlight");
    expect(
      within(row as HTMLElement).getByRole("button", { name: "Steps" }),
    ).toBeInTheDocument();
    expect(
      within(row as HTMLElement).getByRole("button", { name: "Logs" }),
    ).toBeInTheDocument();

    // A state picked on the graph narrows the timeline to that state.
    // (jsdom: React Flow keeps unmeasured cards hidden, so click the card's action.)
    fireEvent.click(cardAction(container, "in_review"));
    expect(await screen.findByText("state in_review")).toBeInTheDocument();
    expect(events()).toEqual(["run-started", "v1:enter", "v1:leave", "v2:enter"]);
    const selected = () =>
      [...container.querySelectorAll('.react-flow__node[data-selected="true"]')].map((node) =>
        node.getAttribute("data-id"),
      );
    expect(selected()).toEqual(["in_review"]);
    // Show all clears the graph's selection too, not only the list's filter.
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(events()).toContain("v3:enter");
    expect(selected()).toEqual([]);
  });

  it("clears the graph's state selection when the run changes", async () => {
    const other = RunSummaryMsg.create({
      ...machineRun,
      runId: "run-n",
      label: "acme/api #44",
    });
    const base = machineApi();
    const api = machineApi({
      getMachineInstance: async (runId) => ({
        ...(await base.getMachineInstance!(runId)),
        run_id: runId,
      }),
      getLatestRunSnapshot: async (runId) =>
        RunSnapshotMsg.create({
          operatorInstanceId: "operator-1",
          asOfEventUlid: eventUlid(1),
          summary: runId === "run-n" ? other : machineRun,
          topology: WorkflowTopologyMsg.create({}),
        }),
    });
    const baseline = await api.loadBaseline();
    api.loadBaseline = async () => ({ ...baseline, runs: [summary, machineRun, other] });
    const workspace = (runId: string) => (
      <WorkflowWorkspace
        api={api}
        workflowId={machineFlow.workflowId}
        layoutStorageKey="timeline-test-run-change"
        navigation={{ selectedRunId: runId, onSelectRun: vi.fn() }}
      />
    );
    const { container, rerender } = render(workspace("run-m"));
    await waitFor(() => expect(events()).toContain("v2:enter"));
    fireEvent.click(cardAction(container, "in_review"));
    expect(await screen.findByText("state in_review")).toBeInTheDocument();
    expect(container.querySelector('.react-flow__node[data-selected="true"]')).not.toBeNull();
    rerender(workspace("run-n"));
    await waitFor(() => expect(screen.queryByText("state in_review")).toBeNull());
    expect(container.querySelector('.react-flow__node[data-selected="true"]')).toBeNull();
  });

  it("keeps the visit and the way back to the state machine on the Logs tab", async () => {
    const { container } = renderRun(machineApi(), machineFlow.workflowId, "run-m");
    await waitFor(() => expect(events()).toContain("v2:enter"));
    const row = container.querySelector('[data-entry-id="v2:enter"]')!;
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /Entered amend/ }));
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "Logs" }));
    expect(
      await screen.findByRole("tab", { name: /Logs/, selected: true }),
    ).toBeInTheDocument();
    const nav = await screen.findByRole("navigation", { name: "Machine visit" });
    expect(nav).toHaveTextContent("Visit 2");
    fireEvent.click(within(nav).getByRole("button", { name: "State machine" }));
    await waitFor(() =>
      expect(screen.queryByRole("navigation", { name: "Machine visit" })).toBeNull(),
    );
  });

  it("shows the reviewed artifact in the open Decide panel, then submits", async () => {
    const card: WaitCard = {
      nodeId: "v3.approve_1",
      kind: "decision",
      state: "waiting",
      schema: {
        type: "object",
        properties: { kind: { enum: ["approve", "reject"] } },
        required: ["kind"],
      },
      prompt: "Merge this PR?",
      show: { diff: "+ fix lint", files: 2 },
    };
    let cards: WaitCard[] = [card];
    const submitDecision = vi.fn(async () => {
      cards = [
        {
          ...card,
          state: "resolved",
          audit: { outcome: { payload_digest: "sha256:ab12" }, reason: "ship it", at: T0 + 40 },
        },
      ];
    });
    // The live visit's snapshot holds the decision step, so its wait is listed.
    const live = {
      ...visitRecord(3, "ready", T0 + 30, null),
      snapshot: RunSnapshotMsg.create({
        operatorInstanceId: "operator-1",
        asOfEventUlid: eventUlid(2),
        summary: RunSummaryMsg.create({ ...machineRun, runId: "run-m:v3" }),
        nodes: [
          NodeSnapshotMsg.create({
            nodeId: "v3.approve_1",
            name: "approve",
            nodeType: "wait",
            status: "running",
            startedAt: 31,
            revision: "1",
          }),
        ],
      }),
    } as MachineVisit;
    const base = machineApi();
    renderRun(
      machineApi({
        listRunWaits: async () => cards,
        submitDecision,
        getMachineVisit: async (runId, visit) =>
          visit === 3 ? live : base.getMachineVisit!(runId, visit),
      }),
      machineFlow.workflowId,
      "run-m",
    );
    const pending = await screen.findByRole("list", { name: "Pending" });
    const row = await within(pending).findByText("Waiting: Merge this PR?");
    expect(screen.queryByRole("region", { name: "Reviewed artifact" })).toBeNull();
    fireEvent.click(within(row.closest("li")!).getByRole("button", { name: "Decide" }));
    // Never answered blind: the pinned artifact is in the open panel.
    const artifact = await screen.findByRole("region", { name: "Reviewed artifact" });
    expect(artifact).toHaveTextContent('"diff": "+ fix lint"');
    expect(artifact).toHaveTextContent('"files": 2');
    fireEvent.change(screen.getByLabelText("kind"), { target: { value: '"approve"' } });
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "ship it" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit decision" }));
    await waitFor(() =>
      expect(submitDecision).toHaveBeenCalledWith(
        "run-m",
        "v3.approve_1",
        { kind: "approve" },
        "ship it",
        expect.any(String),
      ),
    );
    // The resolved wait's entry carries the audit outcome.
    const resolved = await screen.findByText(/Wait resolved: Merge this PR\?/);
    expect(resolved.closest("button")).toHaveTextContent(
      'ship it · outcome {"payload_digest":"sha256:ab12"}',
    );
  });

  it("reads no waits or event facts of a run whose history expired", async () => {
    const expiredRun = RunSummaryMsg.create({ ...machineRun, expired: true });
    const listRunWaits = vi.fn(async () => []);
    const getMachineVisitEvents = vi.fn(async () => ({ items: [], next_cursor: null }));
    const api = machineApi({
      listRunWaits,
      getMachineVisitEvents,
      getLatestRunSnapshot: async () =>
        RunSnapshotMsg.create({
          operatorInstanceId: "operator-1",
          asOfEventUlid: eventUlid(1),
          summary: expiredRun,
        }),
    });
    const baseline = await api.loadBaseline();
    api.loadBaseline = async () => ({ ...baseline, runs: [summary, expiredRun] });
    renderRun(api, machineFlow.workflowId, "run-m");
    await waitFor(() => expect(events()).toContain("v3:enter"));
    expect(listRunWaits).not.toHaveBeenCalled();
    expect(getMachineVisitEvents).not.toHaveBeenCalled();
  });

  it("omits entries a host does not report, with one neutral note", async () => {
    // No event facts, receipts or deferred events (the Delta console's host).
    renderRun(machineApi({}, true), machineFlow.workflowId, "run-m");
    await waitFor(() => expect(events()).toContain("v3:timer:0:armed"));
    const timeline = screen.getByRole("region", { name: "Run timeline" });
    expect(events().some((id) => id?.includes(":fact:") || id?.startsWith("receipt:"))).toBe(
      false,
    );
    expect(timeline.querySelectorAll("[data-note]")).toHaveLength(1);
    expect(
      within(timeline).getByText(
        "Received events, deferred events and send receipts aren't listed for this run.",
      ),
    ).toBeInTheDocument();
    expect(within(timeline).queryByText(/unavailable|not reported|host/i)).toBeNull();
    expect(timeline.querySelector('[data-pending-kind="deferred"]')).toBeNull();
  });

  it("is the same tab for a flow run: steps, and the inspected step filters it", async () => {
    const run = RunSummaryMsg.create({
      ...summary,
      status: "success",
      startedAt: 10,
      endedAt: 13,
      triggeredAt: T0,
    });
    const api = createApi({
      loadBaseline: async () => ({ ...baseline, runs: [run] }),
      getLatestRunSnapshot: async () =>
        RunSnapshotMsg.create({
          ...snapshotFor(run),
          nodes: [
            NodeSnapshotMsg.create({
              nodeId: "fetch",
              name: "Fetch",
              nodeType: "step",
              status: "success",
              startedAt: 10.5,
              endedAt: 12,
              attempts: 2,
              revision: "1",
            }),
          ],
        }),
    });
    const { container } = renderRun(api, workflow.workflowId, run.runId);
    // A flow run opens on its logs; the Timeline tab is beside them.
    const tab = await screen.findByRole("tab", { name: "Timeline" });
    expect(screen.getByRole("tab", { name: /^Logs/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(tab);
    await waitFor(() =>
      expect(events()).toEqual([
        "run-started",
        "step:fetch:start",
        "step:fetch:end",
        "outcome",
      ]),
    );
    expect(screen.getByText("Step Fetch succeeded")).toBeInTheDocument();
    fireEvent.click(cardAction(container, "fetch"));
    expect(await screen.findByText("step Fetch")).toBeInTheDocument();
    expect(events()).toEqual(["step:fetch:start", "step:fetch:end"]);
  });
});
