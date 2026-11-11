import { describe, expect, it } from "vitest";

import type {
  MachineEventReceipt,
  MachineInstance,
  MachineSpec,
  MachineTransition,
  MachineVisit,
  MachineVisitEvent,
} from "./machine";
import { NodeSnapshotMsg, RunSnapshotMsg, RunSummaryMsg } from "./model";
import type { WaitCard } from "./RunWaitPanel";
import {
  arcMatches,
  filterTimeline,
  flowPending,
  flowTimeline,
  machinePending,
  machineTimeline,
  type MachineVisitDetail,
  type TimelineEntry,
} from "./timelineEntries";

// The docs machine: in_review -> amend -> in_review -> ready_to_merge -> closed,
// where ready_to_merge has a timer and amend defers `merged`.
const state = (
  work: string,
  transitions: [string, string][],
  extra: Partial<MachineSpec["states"][string]> = {},
): MachineSpec["states"][string] => ({
  work,
  initial: false,
  final: false,
  defer: [],
  timers: [],
  transitions: transitions.map(([event, target]) => ({ event, target, payload_schema: null })),
  ...extra,
});
const spec: MachineSpec = {
  schema_version: "avalanche.machine/v1",
  name: "pr_lifecycle",
  input_schema: { type: "object" },
  key_fields: ["repo", "number"],
  initial: "in_review",
  states: {
    in_review: state(
      "review_round",
      [
        ["review_done_with_nits", "amend"],
        ["review_done", "ready_to_merge"],
      ],
      { initial: true },
    ),
    amend: state("amend_round", [["amended", "in_review"]], { defer: ["merged"] }),
    ready_to_merge: state(
      "notify_ready",
      [
        ["merged", "closed"],
        ["closed_without_merge", "closed"],
      ],
      { timers: [{ delay_seconds: 600, target: "closed" }] },
    ),
    closed: state("cleanup", [], { final: true }),
  },
  work_graphs: {},
};

const T0 = 1_800_000_000;
const transitions: MachineTransition[] = [
  {
    from: "in_review",
    to: "amend",
    event: "review_done_with_nits",
    source: "internal",
    visit: 1,
  },
  { from: "amend", to: "in_review", event: "amended", source: "external", visit: 2 },
  {
    from: "in_review",
    to: "ready_to_merge",
    event: "review_done",
    source: "internal",
    visit: 3,
  },
  { from: "ready_to_merge", to: "closed", event: "merged", source: "external", visit: 4 },
];

function visitRecord(visit: number, stateName: string, entered: number, left: number | null) {
  return {
    visit,
    state: stateName,
    entered_at: entered,
    left_at: left,
    disposition: left === null ? "current" : "retained",
    transition: transitions.find((item) => item.visit === visit) ?? null,
    nodes: {},
    snapshot: RunSnapshotMsg.create({
      nodes: [
        NodeSnapshotMsg.create({
          nodeId: `v${visit}.work`,
          // Some hosts keep the visit prefix in the step's name.
          name: visit === 2 ? "v2.work" : "work",
          status: "success",
          startedAt: 500 + visit * 10,
          endedAt: 500 + visit * 10 + 1,
          attempts: visit === 1 ? 2 : 1,
        }),
      ],
    }),
  } satisfies MachineVisit;
}

const fact = (
  tseq: number,
  at: number,
  status: MachineVisitEvent["status"],
  event: string,
  receipt: string | null,
  source: MachineVisitEvent["source"] = "external",
): MachineVisitEvent => ({ tseq, at, status, event, receipt_id: receipt, source });

const receipts: MachineEventReceipt[] = [
  {
    receipt_id: "r-amended",
    key: "seed-amended",
    accepted_at: T0 + 25,
    status: "accepted",
    duplicate: false,
  },
  {
    receipt_id: "r-merged",
    key: "seed-merged",
    accepted_at: T0 + 26,
    status: "accepted",
    duplicate: false,
  },
  {
    receipt_id: "r-late",
    key: "seed-late",
    accepted_at: T0 + 42,
    status: "accepted",
    duplicate: false,
  },
];

function details(): Map<number, MachineVisitDetail> {
  return new Map<number, MachineVisitDetail>([
    [
      1,
      {
        record: visitRecord(1, "in_review", T0 + 1, T0 + 20),
        events: [fact(1, T0 + 19, "accepted", "review_done_with_nits", null, "internal")],
      },
    ],
    [
      2,
      {
        record: visitRecord(2, "amend", T0 + 20, T0 + 30),
        events: [
          fact(2, T0 + 25, "accepted", "amended", "r-amended"),
          fact(3, T0 + 26, "accepted", "merged", "r-merged"),
          fact(4, T0 + 27, "ignored", "closed_without_merge", null),
        ],
      },
    ],
    [3, { record: visitRecord(3, "in_review", T0 + 30, T0 + 40), events: [] }],
    [
      4,
      {
        record: visitRecord(4, "ready_to_merge", T0 + 40, T0 + 45),
        // The deferred `merged` is delivered here, in a later visit than it was accepted.
        events: [fact(5, T0 + 45, "consumed", "merged", "r-merged")],
      },
    ],
    [5, { record: visitRecord(5, "closed", T0 + 45, null), events: [] }],
  ]);
}

const instance = (overrides: Partial<MachineInstance> = {}): MachineInstance => ({
  machine: "pr_lifecycle",
  key: ["acme/api", 41],
  run_id: "run-41",
  deployment_id: "",
  status: "success",
  state: "closed",
  visit: 5,
  version: 1,
  accepts: [],
  due: null,
  finished: true,
  receipts,
  pending_events: [],
  ...overrides,
});

function closedRun(overrides: Partial<Parameters<typeof machineTimeline>[0]> = {}) {
  return machineTimeline({
    spec,
    instance: instance(),
    history: transitions,
    visits: [1, 2, 3, 4, 5],
    details: details(),
    startedAt: T0,
    status: "success",
    settled: true,
    fromStart: true,
    ...overrides,
  });
}

const line = (entry: TimelineEntry) => `${entry.kind}: ${entry.title}`;

describe("machine run timeline", () => {
  it("merges visits, steps, events, receipts, timers and the outcome in time order", () => {
    expect(closedRun().map(line)).toEqual([
      "run-started: Run started in in_review",
      "visit-entered: Entered in_review (visit 1)",
      "step-finished: Step work ran in in_review (visit 1)",
      "event: Event review_done_with_nits accepted in in_review → amend",
      "visit-left: Left in_review (visit 1) → amend",
      "visit-entered: Entered amend (visit 2)",
      "step-finished: Step work ran in amend (visit 2)",
      "event: Event amended accepted in amend → in_review",
      "event: Event merged deferred in amend",
      "event: Event closed_without_merge ignored in amend",
      "visit-left: Left amend (visit 2) → in_review",
      "visit-entered: Entered in_review (visit 3)",
      "step-finished: Step work ran in in_review (visit 3)",
      "visit-left: Left in_review (visit 3) → ready_to_merge",
      "visit-entered: Entered ready_to_merge (visit 4)",
      "timer-armed: Timer armed in ready_to_merge → closed",
      "step-finished: Step work ran in ready_to_merge (visit 4)",
      "receipt: Send receipt accepted for key seed-late",
      "event: Event merged delivered in ready_to_merge → closed",
      "timer-cancelled: Timer cancelled in ready_to_merge",
      "visit-left: Left ready_to_merge (visit 4) → closed",
      "visit-entered: Entered closed (visit 5)",
      "step-finished: Step work ran in closed (visit 5)",
      "outcome: Run succeeded in closed",
    ]);
  });

  it("orders entries by time even when the host returns history newest first", () => {
    const shuffled = closedRun({
      history: [...transitions].reverse(),
      visits: [5, 3, 1, 4, 2],
    });
    expect(shuffled.map((entry) => entry.id)).toEqual(closedRun().map((entry) => entry.id));
    const offsets = shuffled.map((entry) => entry.offset ?? 0);
    expect(offsets).toEqual([...offsets].sort((left, right) => left - right));
  });

  it("names each entry's state and arc, and attaches send receipts to their events", () => {
    const entries = closedRun();
    const amended = entries.find(
      (entry) => entry.title === "Event amended accepted in amend → in_review",
    )!;
    expect(amended).toMatchObject({
      state: "amend",
      arc: { from: "amend", to: "in_review", event: "amended" },
      visit: 2,
    });
    expect(amended.detail).toContain("receipt r-amende · key seed-amended");
    const deferred = entries.find((entry) => entry.status === "deferred")!;
    expect(deferred.arc).toBeUndefined();
    expect(deferred.detail).toContain("held for a later state");
    const left = entries.find((entry) => entry.id === "v4:leave")!;
    expect(left.arc).toEqual({
      from: "ready_to_merge",
      to: "closed",
      event: "merged",
      timer: false,
    });
    expect(entries.find((entry) => entry.id === "v2:enter")!.arc).toMatchObject({
      from: "in_review",
      to: "amend",
      event: "review_done_with_nits",
    });
    // Inside a visit a step is named without the visit prefix.
    expect(entries.find((entry) => entry.id === "v2:step:v2.work:end")!.title).toBe(
      "Step work ran in amend (visit 2)",
    );
    // Retries are named on the step.
    expect(entries.find((entry) => entry.id === "v1:step:v1.work:end")!.detail).toBe(
      "ok · after 2 attempts",
    );
    // A receipt no event fact shows stands alone, in the visit its time falls in.
    expect(entries.find((entry) => entry.kind === "receipt")).toMatchObject({
      visit: 4,
      state: "ready_to_merge",
    });
  });

  it("shows a timer that fired, and a cancel, instead of a transition", () => {
    const fired: MachineTransition[] = [
      ...transitions.slice(0, 3),
      { from: "ready_to_merge", to: "closed", event: null, source: "timer", visit: 4 },
    ];
    const timerRun = closedRun({
      history: fired,
      details: new Map(
        [...details()].map(([visit, item]) => [
          visit,
          {
            ...item,
            record: item.record && {
              ...item.record,
              transition: fired.find((t) => t.visit === visit) ?? null,
            },
            events: visit === 4 ? [] : item.events,
          },
        ]),
      ),
    });
    expect(timerRun.map(line)).toContain("timer-fired: Timer fired in ready_to_merge → closed");
    expect(timerRun.find((entry) => entry.kind === "timer-fired")!.arc).toEqual({
      from: "ready_to_merge",
      to: "closed",
      event: null,
      timer: true,
    });
    expect(timerRun.map(line)).not.toContain(
      "timer-cancelled: Timer cancelled in ready_to_merge",
    );

    const cancelled: MachineTransition[] = [
      { from: "in_review", to: null, event: null, source: "cancel", visit: 1 },
    ];
    const cancelRun = machineTimeline({
      spec,
      instance: instance({ visit: 1, state: "in_review", status: "cancelled", finished: true }),
      history: cancelled,
      visits: [1],
      details: new Map([
        [
          1,
          {
            record: {
              ...visitRecord(1, "in_review", T0 + 1, T0 + 9),
              transition: cancelled[0],
            },
          },
        ],
      ]),
      startedAt: T0,
      status: "cancelled",
      settled: true,
      fromStart: true,
    });
    expect(cancelRun.map(line).slice(-1)).toEqual([
      "cancelled: Run cancelled in in_review (visit 1)",
    ]);
    expect(cancelRun.some((entry) => entry.kind === "outcome")).toBe(false);
  });

  it("lists visits it has not read yet from history alone, without times", () => {
    const entries = machineTimeline({
      spec,
      instance: instance({ finished: false, status: "waiting", state: "amend", visit: 2 }),
      history: transitions.slice(0, 1),
      visits: [1, 2],
      details: new Map(),
      settled: false,
      fromStart: true,
      clocksPending: true,
    });
    expect(entries.map(line)).toEqual([
      "run-started: Run started in in_review",
      "visit-entered: Entered in_review (visit 1)",
      "visit-left: Left in_review (visit 1) → amend",
      "visit-entered: Entered amend (visit 2)",
    ]);
    expect(entries.every((entry) => entry.offset === undefined)).toBe(true);
    // An unknown time still says why (the row's "—" carries it as a tooltip).
    expect(entries.every((entry) => (entry.timeNote ?? "").length > 0)).toBe(true);
  });

  it("omits what a host does not report, without placeholders", () => {
    // A foreign host: no event facts, no receipts, no deferred events.
    const foreign = closedRun({
      instance: instance({ receipts: undefined, pending_events: undefined }),
      details: new Map(
        [...details()].map(([visit, item]) => [visit, { record: item.record }] as const),
      ),
    });
    const kinds = new Set(foreign.map((entry) => entry.kind));
    expect(kinds.has("event")).toBe(false);
    expect(kinds.has("receipt")).toBe(false);
    for (const kind of ["visit-entered", "visit-left", "timer-armed", "outcome"] as const)
      expect(kinds.has(kind)).toBe(true);
    expect(
      foreign.some((entry) =>
        /unavailable|not reported/i.test(`${entry.title} ${entry.detail}`),
      ),
    ).toBe(false);
  });
});

describe("machine pending row", () => {
  const live = instance({
    status: "waiting",
    state: "ready_to_merge",
    visit: 4,
    finished: false,
    due: T0 + 640,
    accepts: ["merged", "closed_without_merge"],
    pending_events: [{ event: "merged", receipt_id: "r-merged", payload: null }],
  });
  const decision: WaitCard = {
    nodeId: "v4.approve",
    kind: "decision",
    state: "waiting",
    schema: { type: "object" },
    prompt: "Approve?",
    show: null,
  };

  it("pins the timer, deferred events, open waits and accepted events of a live run", () => {
    const items = machinePending(
      live,
      spec,
      [decision, { ...decision, nodeId: "v3.old" }],
      false,
    );
    expect(items.map((item) => [item.kind, item.title])).toEqual([
      ["timer", "Timer → closed"],
      ["deferred", "Deferred merged"],
      ["wait", "Waiting: Approve?"],
      ["accepts", "Waiting for event"],
    ]);
    expect(items[0]).toMatchObject({
      due: T0 + 640,
      arc: { from: "ready_to_merge", to: "closed", timer: true },
    });
  });

  it("lists a state's timer once: not again as the host's own wait for it", () => {
    // The local operator mirrors ready_to_merge's timer as a sleep wait `v4.after_0`.
    const timerWait: WaitCard = {
      nodeId: "v4.after_0",
      kind: "sleep",
      state: "waiting",
      schema: {},
      prompt: null,
      show: null,
    };
    const titles = machinePending(live, spec, [timerWait], false).map((item) => item.title);
    expect(titles).toEqual(["Timer → closed", "Deferred merged", "Waiting for event"]);
    const entries = closedRun({ waits: [{ ...timerWait, state: "withdrawn" }] });
    expect(entries.filter((entry) => entry.kind.startsWith("wait-"))).toEqual([]);
    expect(entries.map(line)).toContain("timer-armed: Timer armed in ready_to_merge → closed");
  });

  it("pins nothing on a settled run, and claims nothing while the status is unknown", () => {
    expect(machinePending(live, spec, [decision], true)).toEqual([]);
    expect(machinePending({ ...live, finished: true }, spec, [], false)).toEqual([]);
    expect(machinePending(live, spec, [], undefined).map((item) => item.kind)).toEqual([
      "timer",
      "deferred",
    ]);
    // A host without deferred events adds no row for them.
    expect(
      machinePending({ ...live, pending_events: undefined }, spec, [], false).map(
        (item) => item.kind,
      ),
    ).toEqual(["timer", "accepts"]);
  });
});

describe("flow run timeline", () => {
  const run = RunSnapshotMsg.create({
    summary: RunSummaryMsg.create({
      runId: "run-1",
      status: "success",
      startedAt: 100,
      endedAt: 109,
      triggeredAt: T0,
      triggeredBy: "manual",
    }),
    nodes: [
      NodeSnapshotMsg.create({
        nodeId: "review",
        name: "review",
        status: "success",
        startedAt: 103,
        endedAt: 106,
      }),
      NodeSnapshotMsg.create({
        nodeId: "lint",
        name: "lint",
        status: "success",
        startedAt: 101,
        endedAt: 102,
        attempts: 3,
      }),
      NodeSnapshotMsg.create({
        nodeId: "approve",
        name: "approve",
        status: "success",
        startedAt: 106.5,
        endedAt: 108,
      }),
      NodeSnapshotMsg.create({ nodeId: "never", name: "never", status: "pending" }),
    ],
  });
  const waits: WaitCard[] = [
    {
      nodeId: "approve",
      kind: "decision",
      state: "resolved",
      schema: { type: "object" },
      prompt: "Ship it?",
      show: null,
      audit: { outcome: { ok: true }, reason: "looks good", at: T0 + 7.5 },
    },
  ];

  it("lists steps started and finished, retries, waits and the outcome in time order", () => {
    const entries = flowTimeline({ run, waits, settled: true, terminalStatuses: ["success"] });
    expect(entries.map(line)).toEqual([
      "run-started: Run started",
      "step-finished: Step lint ran",
      "step-finished: Step review ran",
      "wait-opened: Wait opened · Ship it?",
      "wait-resolved: Wait resolved · Ship it?",
      "step-finished: Step approve ran",
      "outcome: Run succeeded",
    ]);
    // One row per finished step: how it ended, retries, and how long it took.
    expect(entries.find((entry) => entry.id === "step:lint:end")).toMatchObject({
      detail: "ok · after 3 attempts",
      took: 1,
    });
    expect(entries.find((entry) => entry.id === "step:review:end")!.took).toBe(3);
    // The wait says what it waits for once; the audit says what was decided and why.
    expect(entries.find((entry) => entry.kind === "wait-opened")!.detail).toBe(
      "decision on approve",
    );
    expect(entries.find((entry) => entry.kind === "wait-resolved")).toMatchObject({
      detail: "decided ok: true · reason: looks good · decision on approve",
      took: 1,
    });
    expect(entries.at(-1)!.offset).toBe(9);
    // Every row has a time since the run started, and a wall-clock time for its tooltip.
    expect(entries.every((entry) => entry.offset !== undefined && entry.at !== undefined)).toBe(
      true,
    );
  });

  it("names a decision without a prompt once, not 'decision · decision'", () => {
    const bare: WaitCard = { ...waits[0], prompt: null };
    const entries = flowTimeline({
      run,
      waits: [bare],
      settled: true,
      terminalStatuses: ["success"],
    });
    const opened = entries.find((entry) => entry.kind === "wait-opened")!;
    expect(`${opened.title} · ${opened.detail ?? ""}`).not.toMatch(/decision.*decision/);
    expect(opened.title).toBe("Wait opened · decision on approve");
    expect(opened.detail).toBeUndefined();
    const pending = flowPending([{ ...bare, state: "waiting", audit: undefined }], false)[0];
    expect(pending.title).toBe("Waiting: decision on approve");
    expect(pending.detail).toBeUndefined();
  });

  it("shows a running step as started, with no duration yet", () => {
    const running = flowTimeline({
      run: {
        ...run,
        nodes: [
          NodeSnapshotMsg.create({
            nodeId: "lint",
            name: "lint",
            status: "running",
            startedAt: 101,
          }),
        ],
      },
      settled: false,
      terminalStatuses: ["success"],
    });
    expect(running.find((entry) => entry.kind === "step-started")).toMatchObject({
      title: "Step lint started",
      detail: "running",
      offset: 1,
    });
    expect(running.find((entry) => entry.kind === "step-started")!.took).toBeUndefined();
  });

  it("shows no outcome while the run is live, and pins its open waits", () => {
    const live = flowTimeline({
      run: { ...run, summary: { ...run.summary!, status: "running" } },
      settled: false,
      terminalStatuses: ["success"],
    });
    expect(live.some((entry) => entry.kind === "outcome")).toBe(false);
    const open: WaitCard = { ...waits[0], state: "waiting", audit: undefined };
    expect(flowPending([open], false).map((item) => item.title)).toEqual(["Waiting: Ship it?"]);
    expect(flowPending([open], true)).toEqual([]);
  });
});

describe("graph selection", () => {
  it("filters to a state's entries (in it, or on an arc into or out of it)", () => {
    const amend = filterTimeline(closedRun(), { state: "amend" });
    expect(new Set(amend.map((entry) => entry.visit))).toEqual(new Set([1, 2, 3]));
    expect(
      amend.every(
        (entry) =>
          entry.state === "amend" || entry.arc?.to === "amend" || entry.arc?.from === "amend",
      ),
    ).toBe(true);
    expect(amend.some((entry) => entry.id === "v1:leave")).toBe(true);
    expect(filterTimeline(closedRun(), undefined)).toHaveLength(closedRun().length);
  });

  it("filters to a step node, including a .map item's entries for its parent", () => {
    const entries: TimelineEntry[] = [
      {
        id: "a",
        kind: "step-started",
        title: "",
        tone: "neutral",
        nodeId: "fan",
        sort: [0, 2, 0, 0],
      },
      {
        id: "b",
        kind: "step-started",
        title: "",
        tone: "neutral",
        nodeId: "fan[1]",
        sort: [0, 2, 1, 0],
      },
      {
        id: "c",
        kind: "step-started",
        title: "",
        tone: "neutral",
        nodeId: "fanout",
        sort: [0, 2, 2, 0],
      },
    ];
    expect(filterTimeline(entries, { nodeId: "fan" }).map((entry) => entry.id)).toEqual([
      "a",
      "b",
    ]);
  });

  it("matches the drawn arc of a transition, an event arc or a timer arc", () => {
    const link = (source: string, target: string, label: string, timer?: object) => ({
      source,
      target,
      data: { label, timer },
    });
    const merged = { from: "ready_to_merge", to: "closed", event: "merged" };
    expect(arcMatches(link("ready_to_merge", "closed", "merged"), merged)).toBe(true);
    expect(arcMatches(link("ready_to_merge", "closed", "closed_without_merge"), merged)).toBe(
      false,
    );
    expect(
      arcMatches(link("ready_to_merge", "closed", "after 10m", { delaySeconds: 600 }), {
        from: "ready_to_merge",
        to: "closed",
        event: null,
        timer: true,
      }),
    ).toBe(true);
    expect(
      arcMatches(link("ready_to_merge", "closed", "merged"), { ...merged, to: null }),
    ).toBe(false);
  });
});
