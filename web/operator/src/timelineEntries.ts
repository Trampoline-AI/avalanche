/**
 * One chronological timeline of a run's events, for flow and machine runs alike.
 *
 * Entries come only from the host-neutral OperatorApi reads the run page already
 * makes: the run snapshot (steps, terminal seal), the run's waits, and for a
 * machine run its instance, transition history, visit records and visit event
 * facts. A host that omits an optional read simply contributes no entries of
 * that kind. Every entry names the state, arc or step it belongs to, so the run
 * graph can highlight it and a graph selection can filter the timeline.
 */
import { formatDelay } from "./machineGraph";
import type {
  MachineEventReceipt,
  MachineInstance,
  MachineSpec,
  MachineTransition,
  MachineVisit,
  MachineVisitEvent,
} from "./machine";
import type { NodeSnapshotMsg, RunSnapshotMsg } from "./model";
import type { WaitCard } from "./RunWaitPanel";

export type TimelineKind =
  | "run-started"
  | "visit-entered"
  | "visit-left"
  | "event"
  | "receipt"
  | "timer-armed"
  | "timer-fired"
  | "timer-cancelled"
  | "wait-opened"
  | "wait-resolved"
  | "step-started"
  | "step-finished"
  | "cancelled"
  | "outcome";

export type TimelineTone = "neutral" | "ok" | "warn" | "danger" | "muted";

/** A machine transition arc: `event` names the trigger, `timer` marks a timer arc. */
export interface TimelineArc {
  from: string;
  to: string | null;
  event: string | null;
  timer?: boolean;
}

export interface TimelineEntry {
  /** Stable across re-reads, so hover and selection survive a refresh. */
  id: string;
  kind: TimelineKind;
  /** Seconds since the run started, on one scale for the whole run; absent when unknown. */
  offset?: number;
  /** Wall-clock Unix seconds, when the host reports one for this entry. */
  at?: number;
  title: string;
  detail?: string;
  /** Short outcome word: accepted, deferred, ignored, success, failed, ... */
  status?: string;
  tone: TimelineTone;
  /** Machine visit the entry belongs to. */
  visit?: number;
  /** Machine state the entry belongs to (graph highlight and state filter). */
  state?: string;
  /** Transition arc the entry took or would take (graph highlight). */
  arc?: TimelineArc;
  /** Step node (flow run node ID, or a visit-qualified step `v<visit>.<step>`). */
  nodeId?: string;
  /** Sort key: visit, phase within the visit, offset, sequence. */
  sort: readonly [number, number, number, number];
}

/** Something the run is waiting for now, pinned above the timeline. */
export interface PendingItem {
  id: string;
  kind: "timer" | "deferred" | "accepts" | "wait";
  title: string;
  detail?: string;
  /** Absolute Unix seconds a pending timer fires at. */
  due?: number;
  state?: string;
  arc?: TimelineArc;
  /** A pending wait card (a decision is answered from the pinned row). */
  card?: WaitCard;
  visit?: number;
}

/** What the timeline read of one machine visit holds (absent parts are not loaded). */
export interface MachineVisitDetail {
  record?: MachineVisit;
  events?: MachineVisitEvent[];
  /** Continuation of the visit's event facts; null or absent when complete. */
  eventsCursor?: string | null;
  error?: string;
}

// Phases inside one visit: entry, timer arming, body (steps, events, waits), exit.
const PHASE_ENTER = 0;
const PHASE_ARM = 1;
const PHASE_BODY = 2;
const PHASE_EXIT = 3;
const PHASE_LEAVE = 4;
const PHASE_OUTCOME = 9;

export function compareTimelineEntries(left: TimelineEntry, right: TimelineEntry): number {
  for (let index = 0; index < 4; index++) {
    const difference = left.sort[index] - right.sort[index];
    if (difference !== 0 && !Number.isNaN(difference)) return difference;
  }
  return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
}

/** A step's name inside its visit: without the `v<visit>.` prefix the visit already says. */
function stepName(node: Pick<NodeSnapshotMsg, "name" | "nodeId">, visit?: number) {
  const name = node.name || node.nodeId;
  const prefix = visit === undefined ? "" : `v${visit}.`;
  return prefix && name.startsWith(prefix) ? name.slice(prefix.length) : name;
}

const STEP_DONE: Record<string, { word: string; tone: TimelineTone }> = {
  success: { word: "succeeded", tone: "ok" },
  succeeded: { word: "succeeded", tone: "ok" },
  failed: { word: "failed", tone: "danger" },
  skipped: { word: "skipped", tone: "muted" },
  cancelled: { word: "cancelled", tone: "warn" },
};

/**
 * Step entries of a run (or a visit's) snapshot. `toOffset` maps a node clock
 * reading to the run's offset scale.
 */
function stepEntries(
  nodes: readonly NodeSnapshotMsg[],
  toOffset: (clock: number) => number,
  base: { visit: number; phase: number; state?: string; prefix: string },
): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  nodes.forEach((node, index) => {
    const name = stepName(node, base.visit || undefined);
    const attempts = node.attempts ?? 0;
    const retried = attempts > 1 ? `attempt ${attempts}` : undefined;
    if (node.startedAt > 0) {
      const offset = toOffset(node.startedAt);
      entries.push({
        id: `${base.prefix}step:${node.nodeId}:start`,
        kind: "step-started",
        offset,
        title: `Step ${name} started`,
        detail: retried,
        tone: "neutral",
        visit: base.visit || undefined,
        state: base.state,
        nodeId: node.nodeId,
        sort: [base.visit, base.phase, offset, index * 2],
      });
    }
    const done = STEP_DONE[node.status];
    if (node.endedAt > 0 && done) {
      const offset = toOffset(node.endedAt);
      entries.push({
        id: `${base.prefix}step:${node.nodeId}:end`,
        kind: "step-finished",
        offset,
        title: `Step ${name} ${done.word}`,
        detail:
          [retried && `after ${attempts} attempts`, node.error].filter(Boolean).join(" · ") ||
          undefined,
        status: node.status,
        tone: done.tone,
        visit: base.visit || undefined,
        state: base.state,
        nodeId: node.nodeId,
        sort: [base.visit, base.phase, offset, index * 2 + 1],
      });
    }
  });
  return entries;
}

/**
 * The host's own wait for a state's timer (`v<visit>.after_<index>`, a sleep wait):
 * the timer's entries already tell it (armed, fired, cancelled), so it is not listed again.
 */
function isTimerWait(card: WaitCard, spec: MachineSpec | undefined, state: string | undefined) {
  const match = /^v\d+\.after_(\d+)$/.exec(card.nodeId);
  if (!match || card.kind !== "sleep" || !spec || !state) return false;
  return spec.states[state]?.timers[Number(match[1])] !== undefined;
}

function waitLabel(card: WaitCard) {
  return card.prompt ?? (card.kind === "decision" ? "decision" : `${card.kind} wait`);
}

function waitEntries(
  cards: readonly WaitCard[],
  nodes: ReadonlyMap<string, NodeSnapshotMsg>,
  nodeOffset: (clock: number) => number,
  wallOffset: (at: number) => number,
  base: { visit: number; phase: number; state?: string; prefix: string },
): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  cards.forEach((card, index) => {
    const node = nodes.get(card.nodeId);
    const opened = node && node.startedAt > 0 ? nodeOffset(node.startedAt) : undefined;
    entries.push({
      id: `${base.prefix}wait:${card.nodeId}:open`,
      kind: "wait-opened",
      offset: opened,
      title: `Wait opened: ${waitLabel(card)}`,
      detail: `${card.kind} · ${node ? stepName(node, base.visit || undefined) : card.nodeId}`,
      tone: "neutral",
      visit: base.visit || undefined,
      state: base.state,
      nodeId: card.nodeId,
      sort: [base.visit, base.phase, opened ?? Infinity, 1000 + index * 2],
    });
    if (card.state === "resolved" || card.state === "withdrawn") {
      const closed =
        card.audit !== undefined
          ? wallOffset(card.audit.at)
          : node && node.endedAt > 0
            ? nodeOffset(node.endedAt)
            : undefined;
      entries.push({
        id: `${base.prefix}wait:${card.nodeId}:close`,
        kind: "wait-resolved",
        offset: closed,
        at: card.audit?.at,
        title: `Wait ${card.state}: ${waitLabel(card)}`,
        detail: card.audit
          ? [card.audit.reason, `outcome ${JSON.stringify(card.audit.outcome)}`]
              .filter(Boolean)
              .join(" · ")
          : undefined,
        status: card.state,
        tone: card.state === "resolved" ? "ok" : "muted",
        visit: base.visit || undefined,
        state: base.state,
        nodeId: card.nodeId,
        sort: [base.visit, base.phase, closed ?? Infinity, 1000 + index * 2 + 1],
      });
    }
  });
  return entries;
}

function receiptText(receipt: MachineEventReceipt) {
  return `receipt ${receipt.receipt_id.slice(0, 8)} · key ${receipt.key}${receipt.duplicate ? " · duplicate" : ""}`;
}

function outcomeEntry(
  status: string,
  offset: number | undefined,
  at: number | undefined,
  sortVisit: number,
  detail: string | undefined,
  state?: string,
): TimelineEntry {
  const tone: TimelineTone =
    status === "success" || status === "succeeded"
      ? "ok"
      : status === "failed"
        ? "danger"
        : status === "cancelled"
          ? "warn"
          : "neutral";
  return {
    id: "outcome",
    kind: "outcome",
    offset,
    at,
    title: `Run ${status === "success" ? "succeeded" : status}${state ? ` in ${state}` : ""}`,
    detail,
    status,
    tone,
    state,
    sort: [sortVisit, PHASE_OUTCOME, offset ?? Infinity, 0],
  };
}

export interface FlowTimelineInput {
  run: RunSnapshotMsg;
  waits?: readonly WaitCard[];
  /** The run has settled on its host (not one of its live statuses). */
  settled: boolean;
  /** The host's terminal outcomes (OperatorApi.terminalRunStatuses). */
  terminalStatuses: readonly string[];
}

/** A flow run: start, each step started and finished (with retries), waits, outcome. */
export function flowTimeline({
  run,
  waits = [],
  settled,
  terminalStatuses,
}: FlowTimelineInput): TimelineEntry[] {
  const summary = run.summary;
  const nodes = run.nodes;
  const starts = nodes.map((node) => node.startedAt).filter((value) => value > 0);
  // Node clocks share the run summary's clock; wall clocks share triggeredAt.
  const clockBase = summary?.startedAt || (starts.length ? Math.min(...starts) : 0);
  const wallBase = summary?.triggeredAt || 0;
  const nodeOffset = (clock: number) => Math.max(0, clock - clockBase);
  const wallOffset = (at: number) => (wallBase ? Math.max(0, at - wallBase) : 0);
  const entries: TimelineEntry[] = [
    {
      id: "run-started",
      kind: "run-started",
      offset: 0,
      at: wallBase || undefined,
      title: "Run started",
      detail: summary?.triggeredBy,
      tone: "neutral",
      sort: [0, PHASE_ENTER, 0, 0],
    },
  ];
  entries.push(...stepEntries(nodes, nodeOffset, { visit: 0, phase: PHASE_BODY, prefix: "" }));
  const byId = new Map(nodes.map((node) => [node.nodeId, node] as const));
  entries.push(
    ...waitEntries(waits, byId, nodeOffset, wallOffset, {
      visit: 0,
      phase: PHASE_BODY,
      prefix: "",
    }),
  );
  const seal = run.terminalSeal;
  const status = seal?.terminalStatus ?? summary?.status;
  if (settled && status && (seal || terminalStatuses.includes(status))) {
    const ended = summary?.endedAt ? nodeOffset(summary.endedAt) : undefined;
    const lastStep = entries.reduce<number | undefined>(
      (latest, entry) =>
        entry.offset === undefined ? latest : Math.max(latest ?? 0, entry.offset),
      undefined,
    );
    entries.push(
      outcomeEntry(status, ended ?? lastStep, seal?.timestamp || undefined, 0, seal?.reason),
    );
  }
  return entries.sort(compareTimelineEntries);
}

export interface MachineTimelineInput {
  spec: MachineSpec | undefined;
  instance: MachineInstance;
  /** The loaded window of the run's transitions (any order). */
  history: readonly MachineTransition[];
  /** Visits to show, by number. Others contribute no entries. */
  visits: readonly number[];
  details: ReadonlyMap<number, MachineVisitDetail>;
  waits?: readonly WaitCard[];
  /** Wall-clock Unix seconds the run started (RunSummaryMsg.triggeredAt). */
  startedAt?: number;
  /** The run's own status, and whether it has settled on its host. */
  status?: string;
  settled: boolean;
  /** Whether the shown window starts at the run's first visit (so the start shows). */
  fromStart: boolean;
  /**
   * Visit clocks are still being read: a receipt outside the known visits waits for
   * them rather than showing in the wrong place.
   */
  clocksPending?: boolean;
}

function eventVerdict(
  fact: MachineVisitEvent,
  state: MachineSpec["states"][string] | undefined,
): { word: string; tone: TimelineTone } {
  if (fact.status === "ignored") return { word: "ignored", tone: "warn" };
  if (fact.status === "dropped") return { word: "dropped", tone: "warn" };
  if (fact.status === "consumed") return { word: "delivered", tone: "ok" };
  const event = fact.event ?? "";
  const accepted = state?.transitions.some((transition) => transition.event === event);
  if (!accepted && state?.defer.includes(event)) return { word: "deferred", tone: "neutral" };
  return { word: "accepted", tone: "ok" };
}

/**
 * A machine run: start, then per visit its entry (with the arc it came by), timer
 * arming, steps, events received (accepted, deferred, ignored, dropped) with their
 * send receipts, waits, then how it left (transition, timer fired or cancelled,
 * cancel), and the terminal outcome.
 */
export function machineTimeline(input: MachineTimelineInput): TimelineEntry[] {
  const { spec, instance, details, history, waits = [] } = input;
  const transitions = new Map(history.map((transition) => [transition.visit, transition]));
  const visits = [...input.visits].sort((left, right) => left - right);
  const firstEntered = visits
    .map((visit) => details.get(visit)?.record?.entered_at)
    .find((value): value is number => typeof value === "number");
  const wallBase = input.startedAt || firstEntered || 0;
  const wallOffset = (at: number | null | undefined) =>
    typeof at === "number" && wallBase ? Math.max(0, at - wallBase) : undefined;
  const entries: TimelineEntry[] = [];
  if (input.fromStart)
    entries.push({
      id: "run-started",
      kind: "run-started",
      offset: wallBase ? 0 : undefined,
      at: wallBase || undefined,
      title: "Run started",
      detail: spec ? `initial state ${spec.initial}` : undefined,
      tone: "neutral",
      state: spec?.initial,
      sort: [0, PHASE_ENTER, 0, 0],
    });
  const receipts = new Map((instance.receipts ?? []).map((item) => [item.receipt_id, item]));
  const matchedReceipts = new Set<string>();
  // Which visit accepted each receipt, so a later delivery reads as a deferred one.
  const acceptedIn = new Map<string, number>();
  for (const visit of visits)
    for (const fact of details.get(visit)?.events ?? [])
      if (fact.status === "accepted" && fact.receipt_id) acceptedIn.set(fact.receipt_id, visit);
  const windows: { visit: number; from: number; to: number }[] = [];
  for (const visit of visits) {
    const detail = details.get(visit);
    const record = detail?.record;
    const transition = record?.transition ?? transitions.get(visit) ?? null;
    const state =
      (record && record.disposition !== "expired" ? record.state : undefined) ||
      transition?.from ||
      (visit === instance.visit ? (instance.state ?? undefined) : undefined);
    if (!state) continue;
    const declaration = spec?.states[state];
    const previous = details.get(visit - 1)?.record?.transition ?? transitions.get(visit - 1);
    const entered = wallOffset(record?.entered_at);
    const left = wallOffset(record?.left_at);
    if (record?.entered_at != null)
      windows.push({ visit, from: record.entered_at, to: record.left_at ?? Infinity });
    const current = visit === instance.visit && !instance.finished;
    entries.push({
      id: `v${visit}:enter`,
      kind: "visit-entered",
      offset: entered,
      at: record?.entered_at ?? undefined,
      title: `Entered ${state}`,
      detail: [
        `visit ${visit}`,
        previous ? `via ${previous.event ?? previous.source}` : undefined,
        record?.disposition === "expired" ? "history expired" : undefined,
        current ? "current" : undefined,
      ]
        .filter(Boolean)
        .join(" · "),
      tone: "neutral",
      visit,
      state,
      arc: previous
        ? {
            from: previous.from,
            to: state,
            event: previous.event,
            timer: previous.source === "timer",
          }
        : undefined,
      sort: [visit, PHASE_ENTER, entered ?? 0, 0],
    });
    (declaration?.timers ?? []).forEach((timer, index) => {
      const due =
        record?.entered_at != null ? record.entered_at + timer.delay_seconds : undefined;
      entries.push({
        id: `v${visit}:timer:${index}:armed`,
        kind: "timer-armed",
        offset: entered,
        at: record?.entered_at ?? undefined,
        title: `Timer armed → ${timer.target}`,
        detail: `after ${formatDelay(timer.delay_seconds)}${due !== undefined ? ` · due ${new Date(due * 1000).toLocaleTimeString()}` : ""}`,
        tone: "neutral",
        visit,
        state,
        arc: { from: state, to: timer.target, event: null, timer: true },
        sort: [visit, PHASE_ARM, entered ?? 0, index],
      });
    });
    if (record?.snapshot && record.disposition !== "expired") {
      const nodes = record.snapshot.nodes;
      const starts = nodes.map((node) => node.startedAt).filter((value) => value > 0);
      // A visit's steps start as the visit is entered: anchor their clock there.
      const anchor = starts.length ? Math.min(...starts) : 0;
      const nodeOffset = (clock: number) => (entered ?? 0) + Math.max(0, clock - anchor);
      const scope = { visit, phase: PHASE_BODY, state, prefix: `v${visit}:` };
      entries.push(...stepEntries(nodes, nodeOffset, scope));
      const byId = new Map(nodes.map((node) => [node.nodeId, node] as const));
      entries.push(
        ...waitEntries(
          waits.filter(
            (card) => card.nodeId.startsWith(`v${visit}.`) && !isTimerWait(card, spec, state),
          ),
          byId,
          nodeOffset,
          (at) => wallOffset(at) ?? 0,
          scope,
        ),
      );
    }
    for (const fact of detail?.events ?? []) {
      // A directly accepted event's delivery is the transition itself; only a
      // delivery of an event accepted (deferred) in an earlier visit is shown.
      if (fact.status === "consumed") {
        const acceptedVisit = fact.receipt_id ? acceptedIn.get(fact.receipt_id) : undefined;
        if (acceptedVisit === undefined || acceptedVisit === visit) continue;
      }
      const verdict = eventVerdict(fact, declaration);
      const receipt = fact.receipt_id ? receipts.get(fact.receipt_id) : undefined;
      if (receipt && fact.status === "accepted") matchedReceipts.add(receipt.receipt_id);
      const target = declaration?.transitions.find(
        (transition) => transition.event === fact.event,
      )?.target;
      const offset = wallOffset(fact.at);
      entries.push({
        id: `v${visit}:fact:${fact.tseq}`,
        kind: "event",
        offset,
        at: fact.at,
        title: `Event ${fact.event ?? "—"} ${verdict.word}`,
        detail: [
          fact.source,
          receipt && fact.status === "accepted" ? receiptText(receipt) : undefined,
          verdict.word === "deferred" ? "held for a later state" : undefined,
        ]
          .filter(Boolean)
          .join(" · "),
        status: verdict.word,
        tone: verdict.tone,
        visit,
        state,
        arc:
          target !== undefined && verdict.word !== "ignored" && verdict.word !== "dropped"
            ? { from: state, to: target, event: fact.event }
            : undefined,
        sort: [visit, PHASE_BODY, offset ?? Infinity, fact.tseq],
      });
    }
    if (!transition) continue;
    const hasTimers = (declaration?.timers.length ?? 0) > 0;
    if (transition.source === "timer") {
      entries.push({
        id: `v${visit}:timer:fired`,
        kind: "timer-fired",
        offset: left,
        at: record?.left_at ?? undefined,
        title: `Timer fired → ${transition.to ?? "closed"}`,
        tone: "neutral",
        visit,
        state,
        arc: { from: state, to: transition.to, event: null, timer: true },
        sort: [visit, PHASE_EXIT, left ?? Infinity, 0],
      });
    } else if (hasTimers) {
      entries.push({
        id: `v${visit}:timer:cancelled`,
        kind: "timer-cancelled",
        offset: left,
        at: record?.left_at ?? undefined,
        title: "Timer cancelled",
        detail: `left ${state} first`,
        tone: "muted",
        visit,
        state,
        sort: [visit, PHASE_EXIT, left ?? Infinity, 0],
      });
    }
    if (transition.source === "cancel") {
      entries.push({
        id: `v${visit}:cancel`,
        kind: "cancelled",
        offset: left,
        at: record?.left_at ?? undefined,
        title: `Run cancelled in ${state}`,
        tone: "warn",
        visit,
        state,
        sort: [visit, PHASE_LEAVE, left ?? Infinity, 0],
      });
      continue;
    }
    entries.push({
      id: `v${visit}:leave`,
      kind: "visit-left",
      offset: left,
      at: record?.left_at ?? undefined,
      title: `Left ${state} → ${transition.to ?? "closed"}`,
      detail: `on ${transition.event ?? (transition.source === "timer" ? "timer" : transition.source)} · ${transition.source}`,
      tone: "neutral",
      visit,
      state,
      arc: {
        from: state,
        to: transition.to,
        event: transition.event,
        timer: transition.source === "timer",
      },
      sort: [visit, PHASE_LEAVE, left ?? Infinity, 0],
    });
  }
  // Receipts the event facts do not show (not read, or not reported) stand alone,
  // in the visit whose clocks contain them.
  const lastVisit = visits.at(-1) ?? instance.visit;
  for (const receipt of instance.receipts ?? []) {
    if (matchedReceipts.has(receipt.receipt_id)) continue;
    if (acceptedIn.has(receipt.receipt_id)) continue;
    const window = windows.find(
      (item) => receipt.accepted_at >= item.from && receipt.accepted_at < item.to,
    );
    const visit =
      window?.visit ?? (windows.length || input.clocksPending ? undefined : lastVisit);
    // Outside the shown visits (an older window): not this window's entry.
    if (visit === undefined) continue;
    const offset = wallOffset(receipt.accepted_at);
    entries.push({
      id: `receipt:${receipt.receipt_id}`,
      kind: "receipt",
      offset,
      at: receipt.accepted_at,
      title: `Send receipt ${receipt.status}`,
      detail: receiptText(receipt),
      status: receipt.status,
      tone: receipt.status === "accepted" ? "ok" : "warn",
      visit,
      state: entries.find((entry) => entry.visit === visit)?.state,
      sort: [visit, PHASE_BODY, offset ?? Infinity, 10_000],
    });
  }
  const finalVisit = visits.includes(instance.visit);
  if (finalVisit && (input.settled || instance.finished)) {
    const status = input.status ?? instance.status;
    if (status && !entries.some((entry) => entry.kind === "cancelled")) {
      const record = details.get(instance.visit)?.record;
      // Settled once the final visit's work is done: no earlier than its last entry.
      const offset = entries.reduce<number | undefined>(
        (latest, entry) =>
          entry.offset === undefined ? latest : Math.max(latest ?? 0, entry.offset),
        wallOffset(record?.left_at ?? record?.entered_at),
      );
      entries.push(
        outcomeEntry(
          status,
          offset,
          undefined,
          instance.visit,
          undefined,
          instance.state ?? undefined,
        ),
      );
    }
  }
  return entries.sort(compareTimelineEntries);
}

/** What a machine run waits for now: its timer, deferred events, accepted events, waits. */
export function machinePending(
  instance: MachineInstance | undefined,
  spec: MachineSpec | undefined,
  waits: readonly WaitCard[],
  settled: boolean | undefined,
): PendingItem[] {
  if (!instance || settled === true || instance.finished) return [];
  const items: PendingItem[] = [];
  const state = instance.state ?? undefined;
  const declaration = state ? spec?.states[state] : undefined;
  if (instance.due !== null && state) {
    const timer = declaration?.timers[0];
    items.push({
      id: "timer",
      kind: "timer",
      title: `Timer → ${timer?.target ?? "next state"}`,
      due: instance.due,
      state,
      arc: timer ? { from: state, to: timer.target, event: null, timer: true } : undefined,
    });
  }
  for (const deferred of instance.pending_events ?? [])
    items.push({
      id: `deferred:${deferred.receipt_id}`,
      kind: "deferred",
      title: `Deferred ${deferred.event}`,
      detail: `receipt ${deferred.receipt_id.slice(0, 8)}`,
      state,
    });
  for (const card of waits)
    if (
      (card.state === "pending" || card.state === "waiting") &&
      card.nodeId.startsWith(`v${instance.visit}.`) &&
      !isTimerWait(card, spec, state)
    )
      items.push({
        id: `wait:${card.nodeId}`,
        kind: "wait",
        title: `Waiting: ${waitLabel(card)}`,
        detail: card.kind,
        state,
        card,
        visit: instance.visit,
      });
  if (instance.accepts.length > 0 && settled === false)
    items.push({
      id: "accepts",
      kind: "accepts",
      title: "Waiting for event",
      detail: instance.accepts.join(", "),
      state,
    });
  return items;
}

/** What a flow run waits for now: its open waits. */
export function flowPending(waits: readonly WaitCard[], settled: boolean): PendingItem[] {
  if (settled) return [];
  return waits
    .filter((card) => card.state === "pending" || card.state === "waiting")
    .map((card) => ({
      id: `wait:${card.nodeId}`,
      kind: "wait" as const,
      title: `Waiting: ${waitLabel(card)}`,
      detail: card.kind,
      card,
    }));
}

/** A graph selection that narrows the timeline: a machine state or a step node. */
export interface TimelineFilter {
  state?: string;
  nodeId?: string;
}

/** Entries of a state (in it, or on an arc into or out of it) or of a step node. */
export function filterTimeline(
  entries: readonly TimelineEntry[],
  filter: TimelineFilter | undefined,
): TimelineEntry[] {
  if (!filter || (filter.state === undefined && filter.nodeId === undefined))
    return [...entries];
  return entries.filter((entry) => {
    if (filter.nodeId !== undefined)
      return (
        entry.nodeId === filter.nodeId ||
        (entry.nodeId?.startsWith(`${filter.nodeId}[`) ?? false)
      );
    return (
      entry.state === filter.state ||
      entry.arc?.from === filter.state ||
      entry.arc?.to === filter.state
    );
  });
}

/** What the graph highlights for an entry: its state, arc and step node. */
export interface GraphHighlight {
  state?: string;
  arc?: TimelineArc;
  nodeId?: string;
}

export function entryHighlight(
  entry: Pick<TimelineEntry, "state" | "arc" | "nodeId"> | undefined,
): GraphHighlight | undefined {
  if (!entry) return undefined;
  const { state, arc, nodeId } = entry;
  if (state === undefined && arc === undefined && nodeId === undefined) return undefined;
  return { state, arc, nodeId };
}

/** Whether a drawn machine arc is the highlighted transition. */
export function arcMatches(
  link: { source: string; target: string; data: { label: string; timer?: unknown } },
  arc: TimelineArc | undefined,
): boolean {
  if (!arc || arc.to === null) return false;
  if (link.source !== arc.from || link.target !== arc.to) return false;
  return arc.timer ? link.data.timer !== undefined : link.data.label === arc.event;
}
