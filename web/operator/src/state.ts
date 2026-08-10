import { useCallback, useEffect, useReducer, useRef } from "react";

import type { OperatorApi, StructuralBaseline } from "./api";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./generated/operator";

const MAX_PENDING_ENVELOPES = 1024;
const MAX_ENVELOPES_PER_FRAME = 256;
const MAX_LIVE_DESCRIPTORS = 256;
const INITIAL_RETRY_MILLISECONDS = 250;
const MAX_RETRY_MILLISECONDS = 1000;

export type SelectedRunStatus = "idle" | "loading" | "ready" | "error";

export interface OperatorProjection {
  catalog?: CatalogSnapshotMsg;
  runs: Record<string, RunSummaryMsg>;
  selectedRunId?: string;
  selectedRun?: RunSnapshotMsg;
  selectedRunStatus: SelectedRunStatus;
  selectedRunError?: string;
  liveEvents: Record<string, AgentEventDescriptorMsg[]>;
  liveLogs: Record<string, LogRecordDescriptorMsg[]>;
  liveEventRepairWatermarks: Record<string, string>;
  liveLogRepairWatermarks: Record<string, string>;
  operatorInstanceId: string;
  sequence: string;
  connection: "connecting" | "live" | "reconnecting";
  workflowReloading: boolean;
  error?: string;
  action?: { kind: "start" | "cancel"; target: string };
}

type ProjectionAction =
  | { type: "baseline"; baseline: StructuralBaseline }
  | { type: "envelopes"; envelopes: OperatorUpdateEnvelope[] }
  | { type: "connection"; connection: OperatorProjection["connection"]; error?: string }
  | { type: "action"; action?: OperatorProjection["action"] }
  | { type: "selectionLoading"; runId: string }
  | { type: "selectionReady"; runId: string; snapshot: RunSnapshotMsg }
  | { type: "selectionError"; runId: string; error: string }
  | { type: "selectionCleared" };

export const emptyProjection: OperatorProjection = {
  runs: {},
  selectedRunStatus: "idle",
  liveEvents: {},
  liveLogs: {},
  liveEventRepairWatermarks: {},
  liveLogRepairWatermarks: {},
  operatorInstanceId: "",
  sequence: "0",
  connection: "connecting",
  workflowReloading: false,
};

interface BoundedAppend<T> {
  items: T[];
  droppedThrough?: string;
}

function appendBounded<T>(
  current: T[],
  item: T,
  sequenceOf: (value: T) => string,
): BoundedAppend<T> {
  const sequence = BigInt(sequenceOf(item));
  if (current.some((value) => BigInt(sequenceOf(value)) === sequence)) return { items: current };

  let insertion = current.length;
  while (insertion > 0 && BigInt(sequenceOf(current[insertion - 1])) > sequence) insertion -= 1;
  const ordered = [...current.slice(0, insertion), item, ...current.slice(insertion)];
  if (ordered.length <= MAX_LIVE_DESCRIPTORS) return { items: ordered };

  const dropped = ordered.length - MAX_LIVE_DESCRIPTORS;
  return {
    items: ordered.slice(dropped),
    droppedThrough: sequenceOf(ordered[dropped - 1]),
  };
}

function laterWatermark(current: string | undefined, candidate: string): string {
  return current === undefined || BigInt(candidate) > BigInt(current) ? candidate : current;
}

function snapshotCanCommit(
  state: OperatorProjection,
  runId: string,
  snapshot: RunSnapshotMsg,
): boolean {
  if (
    snapshot.operatorInstanceId !== state.operatorInstanceId ||
    snapshot.summary?.runId !== runId ||
    BigInt(snapshot.asOfSequence) < BigInt(state.sequence)
  ) {
    return false;
  }
  const projectedSummary = state.runs[runId];
  return (
    projectedSummary === undefined ||
    BigInt(snapshot.summary.revision) >= BigInt(projectedSummary.revision)
  );
}

function withoutRunBuckets<T>(
  buckets: Record<string, T>,
  runId: string,
): Record<string, T> {
  const prefix = `${runId}:`;
  return Object.fromEntries(Object.entries(buckets).filter(([key]) => !key.startsWith(prefix)));
}

function withoutKey<T>(buckets: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(buckets).filter(([candidate]) => candidate !== key));
}

function applyEnvelope(
  state: OperatorProjection,
  envelope: OperatorUpdateEnvelope,
): OperatorProjection {
  if (envelope.operatorInstanceId !== state.operatorInstanceId) {
    throw new Error("Operator epoch changed");
  }
  if (envelope.payload.oneofKind !== "update") {
    throw new Error("Operator requested a structural reset");
  }
  const update = envelope.payload.update;
  if (BigInt(update.sequence) !== BigInt(state.sequence) + 1n) {
    throw new Error(`Operator update gap after sequence ${state.sequence}`);
  }

  const next: OperatorProjection = { ...state, sequence: update.sequence, error: undefined };
  const change = update.change;
  if (change.oneofKind === "workflowReloadStatus") {
    next.workflowReloading = change.workflowReloadStatus.reloading;
    return next;
  }
  if (change.oneofKind === "catalogReplaced") {
    if (change.catalogReplaced.catalog) next.catalog = change.catalogReplaced.catalog;
    return next;
  }
  if (change.oneofKind === "runCreated" && change.runCreated.summary) {
    const summary = change.runCreated.summary;
    next.runs = { ...state.runs, [summary.runId]: summary };
    return next;
  }

  const runId =
    change.oneofKind === "runStatusChanged"
      ? change.runStatusChanged.runId
      : change.oneofKind === "nodeStatusChanged"
        ? change.nodeStatusChanged.runId
        : change.oneofKind === "logAppended"
          ? change.logAppended.runId
          : change.oneofKind === "agentEventAppended"
            ? change.agentEventAppended.runId
            : change.oneofKind === "traceFinalized"
              ? change.traceFinalized.runId
              : "";
  const selectedSnapshot = state.selectedRunId === runId ? state.selectedRun : undefined;
  const selected =
    selectedSnapshot && BigInt(update.sequence) > BigInt(selectedSnapshot.asOfSequence)
      ? selectedSnapshot
      : undefined;

  if (change.oneofKind === "runStatusChanged") {
    const changed = change.runStatusChanged;
    const summary = state.runs[runId];
    if (summary) {
      next.runs = {
        ...state.runs,
        [runId]: {
          ...summary,
          status: changed.status,
          startedAt: changed.startedAt,
          endedAt: changed.endedAt,
          revision: changed.revision,
        },
      };
    }
    if (selected?.summary) {
      next.selectedRun = {
        ...selected,
        summary: {
          ...selected.summary,
          status: changed.status,
          startedAt: changed.startedAt,
          endedAt: changed.endedAt,
          revision: changed.revision,
        },
      };
    }
  } else if (change.oneofKind === "nodeStatusChanged" && selected) {
    const changed = change.nodeStatusChanged;
    next.selectedRun = {
      ...selected,
      nodes: selected.nodes.map((node) =>
        node.nodeId === changed.nodeId
          ? {
              ...node,
              status: changed.status,
              startedAt: changed.startedAt,
              endedAt: changed.endedAt,
              revision: changed.revision,
              error: changed.error,
            }
          : node,
      ),
    };
  } else if (
    change.oneofKind === "logAppended" &&
    state.selectedRunId === runId &&
    (selectedSnapshot === undefined ||
      BigInt(update.sequence) > BigInt(selectedSnapshot.asOfSequence)) &&
    change.logAppended.log
  ) {
    const log = change.logAppended.log;
    const key = runId;
    const appended = appendBounded(state.liveLogs[key] ?? [], log, (value) => value.sequence);
    if (appended.items !== state.liveLogs[key]) {
      next.liveLogs = { ...state.liveLogs, [key]: appended.items };
    }
    if (appended.droppedThrough !== undefined) {
      next.liveLogRepairWatermarks = {
        ...state.liveLogRepairWatermarks,
        [key]: laterWatermark(state.liveLogRepairWatermarks[key], appended.droppedThrough),
      };
    }
  } else if (
    change.oneofKind === "agentEventAppended" &&
    state.selectedRunId === runId &&
    (selectedSnapshot === undefined ||
      BigInt(update.sequence) > BigInt(selectedSnapshot.asOfSequence)) &&
    change.agentEventAppended.event
  ) {
    const event = change.agentEventAppended.event;
    const key = `${runId}:${change.agentEventAppended.nodeId}`;
    const appended = appendBounded(
      state.liveEvents[key] ?? [],
      event,
      (value) => value.eventSequence,
    );
    if (appended.items !== state.liveEvents[key]) {
      next.liveEvents = { ...state.liveEvents, [key]: appended.items };
    }
    if (appended.droppedThrough !== undefined) {
      next.liveEventRepairWatermarks = {
        ...state.liveEventRepairWatermarks,
        [key]: laterWatermark(state.liveEventRepairWatermarks[key], appended.droppedThrough),
      };
    }
  } else if (change.oneofKind === "traceFinalized" && selected && change.traceFinalized.trace) {
    next.selectedRun = {
      ...selected,
      nodes: selected.nodes.map((node) =>
        node.nodeId === change.traceFinalized.nodeId
          ? { ...node, trace: change.traceFinalized.trace }
          : node,
      ),
    };
  }
  return next;
}

export function projectionReducer(
  state: OperatorProjection,
  action: ProjectionAction,
): OperatorProjection {
  if (action.type === "baseline") {
    return {
      ...emptyProjection,
      catalog: action.baseline.catalog,
      runs: Object.fromEntries(action.baseline.runs.map((run) => [run.runId, run])),
      operatorInstanceId: action.baseline.catalog.operatorInstanceId,
      sequence: action.baseline.asOfSequence,
      connection: "live",
    };
  }
  if (action.type === "connection") {
    return { ...state, connection: action.connection, error: action.error };
  }
  if (action.type === "action") return { ...state, action: action.action };
  if (action.type === "selectionLoading") {
    return {
      ...state,
      selectedRunId: action.runId,
      selectedRun: undefined,
      selectedRunStatus: "loading",
      selectedRunError: undefined,
      liveEvents: {},
      liveLogs: {},
      liveEventRepairWatermarks: {},
      liveLogRepairWatermarks: {},
    };
  }
  if (action.type === "selectionReady") {
    if (
      state.selectedRunId !== action.runId ||
      !snapshotCanCommit(state, action.runId, action.snapshot)
    ) {
      return state;
    }
    return {
      ...state,
      selectedRunId: action.runId,
      selectedRun: action.snapshot,
      selectedRunStatus: "ready",
      selectedRunError: undefined,
      liveEvents: withoutRunBuckets(state.liveEvents, action.runId),
      liveLogs: withoutKey(state.liveLogs, action.runId),
      liveEventRepairWatermarks: withoutRunBuckets(
        state.liveEventRepairWatermarks,
        action.runId,
      ),
      liveLogRepairWatermarks: withoutKey(state.liveLogRepairWatermarks, action.runId),
    };
  }
  if (action.type === "selectionError") {
    return {
      ...state,
      selectedRunId: action.runId,
      selectedRun: undefined,
      selectedRunStatus: "error",
      selectedRunError: action.error,
    };
  }
  if (action.type === "selectionCleared") {
    return {
      ...state,
      selectedRunId: undefined,
      selectedRun: undefined,
      selectedRunStatus: "idle",
      selectedRunError: undefined,
      liveEvents: {},
      liveLogs: {},
      liveEventRepairWatermarks: {},
      liveLogRepairWatermarks: {},
    };
  }

  let next = state;
  for (const envelope of action.envelopes) next = applyEnvelope(next, envelope);
  return next;
}

export function useOperatorProjection(api: OperatorApi) {
  const [state, dispatch] = useReducer(projectionReducer, emptyProjection);
  const stateRef = useRef(state);
  stateRef.current = state;
  const projectionCursor = useRef({
    operatorInstanceId: state.operatorInstanceId,
    sequence: state.sequence,
  });
  if (
    projectionCursor.current.operatorInstanceId !== state.operatorInstanceId ||
    BigInt(state.sequence) > BigInt(projectionCursor.current.sequence)
  ) {
    projectionCursor.current = {
      operatorInstanceId: state.operatorInstanceId,
      sequence: state.sequence,
    };
  }

  const cycleController = useRef<AbortController | undefined>(undefined);
  const selectionController = useRef<AbortController | undefined>(undefined);
  const selectionGeneration = useRef(0);
  const pendingFrame = useRef<number | undefined>(undefined);
  const retryTimer = useRef<number | undefined>(undefined);
  const wakeRetry = useRef<(() => void) | undefined>(undefined);

  const abortSelection = useCallback(() => {
    selectionGeneration.current += 1;
    selectionController.current?.abort();
    selectionController.current = undefined;
  }, []);

  const reconcile = useCallback(() => {
    cycleController.current?.abort();
    wakeRetry.current?.();
  }, []);

  useEffect(() => {
    const lifecycle = new AbortController();
    let retryMilliseconds = INITIAL_RETRY_MILLISECONDS;

    const waitForRetry = (milliseconds: number) =>
      new Promise<void>((resolve) => {
        const finish = () => {
          if (retryTimer.current !== undefined) window.clearTimeout(retryTimer.current);
          retryTimer.current = undefined;
          if (wakeRetry.current === finish) wakeRetry.current = undefined;
          resolve();
        };
        wakeRetry.current = finish;
        retryTimer.current = window.setTimeout(finish, milliseconds);
      });

    const run = async () => {
      while (!lifecycle.signal.aborted) {
        const cycle = new AbortController();
        cycleController.current = cycle;
        let pending: OperatorUpdateEnvelope[] = [];
        let retryAfterCycle = 0;

        const clearPending = () => {
          pending = [];
          if (pendingFrame.current !== undefined) {
            window.cancelAnimationFrame(pendingFrame.current);
            pendingFrame.current = undefined;
          }
        };
        const scheduleFrame = () => {
          if (pendingFrame.current !== undefined || cycle.signal.aborted) return;
          pendingFrame.current = window.requestAnimationFrame(() => {
            pendingFrame.current = undefined;
            if (cycle.signal.aborted) {
              pending = [];
              return;
            }
            const envelopes = pending.splice(0, MAX_ENVELOPES_PER_FRAME);
            if (envelopes.length > 0) {
              const latest = envelopes[envelopes.length - 1];
              if (latest.payload.oneofKind === "update") {
                projectionCursor.current = {
                  operatorInstanceId: latest.operatorInstanceId,
                  sequence: latest.payload.update.sequence,
                };
              }
              dispatch({ type: "envelopes", envelopes });
            }
            if (pending.length > 0) scheduleFrame();
          });
        };

        try {
          console.info("Connecting to Avalanche operator");
          dispatch({ type: "connection", connection: "connecting" });
          const baseline = await api.loadBaseline(cycle.signal);
          if (cycle.signal.aborted || lifecycle.signal.aborted) continue;
          abortSelection();
          projectionCursor.current = {
            operatorInstanceId: baseline.catalog.operatorInstanceId,
            sequence: baseline.asOfSequence,
          };
          dispatch({ type: "baseline", baseline });
          console.info("Connected to Avalanche operator");
          retryMilliseconds = INITIAL_RETRY_MILLISECONDS;

          let expectedSequence = baseline.asOfSequence;
          for await (const envelope of api.streamUpdates(
            baseline.catalog.operatorInstanceId,
            expectedSequence,
            cycle.signal,
          )) {
            if (cycle.signal.aborted || lifecycle.signal.aborted) break;
            if (
              envelope.operatorInstanceId !== baseline.catalog.operatorInstanceId ||
              envelope.payload.oneofKind !== "update" ||
              BigInt(envelope.payload.update.sequence) !== BigInt(expectedSequence) + 1n ||
              pending.length >= MAX_PENDING_ENVELOPES
            ) {
              cycle.abort();
              break;
            }
            pending.push(envelope);
            expectedSequence = envelope.payload.update.sequence;
            scheduleFrame();
          }
          clearPending();
          if (!lifecycle.signal.aborted) {
            console.warn("Avalanche operator update stream ended; reconnecting");
            dispatch({ type: "connection", connection: "reconnecting" });
          }
        } catch (error) {
          clearPending();
          if (!lifecycle.signal.aborted && !cycle.signal.aborted) {
            dispatch({
              type: "connection",
              connection: "reconnecting",
              error: error instanceof Error ? error.message : "Operator connection failed",
            });
            retryAfterCycle = retryMilliseconds;
            console.warn(
              `Unable to connect to Avalanche operator; retrying in ${retryAfterCycle} ms.`,
              error,
            );
            retryMilliseconds = Math.min(
              retryMilliseconds * 2,
              MAX_RETRY_MILLISECONDS,
            );
          }
        } finally {
          clearPending();
          cycle.abort();
          if (cycleController.current === cycle) cycleController.current = undefined;
        }

        if (retryAfterCycle > 0 && !lifecycle.signal.aborted) {
          await waitForRetry(retryAfterCycle);
        }
      }
    };

    void run();
    return () => {
      lifecycle.abort();
      cycleController.current?.abort();
      if (pendingFrame.current !== undefined) {
        window.cancelAnimationFrame(pendingFrame.current);
        pendingFrame.current = undefined;
      }
      wakeRetry.current?.();
      abortSelection();
    };
  }, [abortSelection, api]);

  const loadSelectedRun = useCallback(
    async (runId: string, showLoading: boolean) => {
      abortSelection();
      const generation = selectionGeneration.current;
      const controller = new AbortController();
      selectionController.current = controller;
      const operatorInstanceId = stateRef.current.operatorInstanceId;
      if (showLoading) dispatch({ type: "selectionLoading", runId });
      try {
        while (!controller.signal.aborted && selectionGeneration.current === generation) {
          const snapshot = await api.getLatestRunSnapshot(
            runId,
            operatorInstanceId,
            controller.signal,
          );
          if (
            controller.signal.aborted ||
            selectionGeneration.current !== generation ||
            stateRef.current.operatorInstanceId !== operatorInstanceId
          ) {
            return;
          }
          if (
            projectionCursor.current.operatorInstanceId !== operatorInstanceId ||
            BigInt(snapshot.asOfSequence) < BigInt(projectionCursor.current.sequence)
          ) {
            continue;
          }
          if (!snapshotCanCommit(stateRef.current, runId, snapshot)) continue;
          dispatch({ type: "selectionReady", runId, snapshot });
          return;
        }
      } catch (error) {
        if (controller.signal.aborted || selectionGeneration.current !== generation) return;
        if (showLoading) {
          dispatch({
            type: "selectionError",
            runId,
            error: error instanceof Error ? error.message : "Run snapshot failed",
          });
        }
      } finally {
        if (selectionGeneration.current === generation) selectionController.current = undefined;
      }
    },
    [abortSelection, api],
  );

  const selectRun = useCallback(
    async (runId?: string) => {
      if (runId === undefined) {
        abortSelection();
        dispatch({ type: "selectionCleared" });
        return;
      }
      await loadSelectedRun(runId, true);
    },
    [abortSelection, loadSelectedRun],
  );

  const selectedRunId = state.selectedRunId;
  const repairWatermark =
    selectedRunId && state.selectedRunStatus === "ready"
      ? [
          ...Object.entries(state.liveEventRepairWatermarks)
            .filter(([key]) => key.startsWith(`${selectedRunId}:`))
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([key, watermark]) => `${key}:${watermark}`),
          ...(state.liveLogRepairWatermarks[selectedRunId]
            ? [`${selectedRunId}:${state.liveLogRepairWatermarks[selectedRunId]}`]
            : []),
        ].join("|")
      : "";

  useEffect(() => {
    if (!selectedRunId || !repairWatermark) return;
    void loadSelectedRun(selectedRunId, false);
  }, [loadSelectedRun, repairWatermark, selectedRunId]);

  const startRun = useCallback(
    async (workflowSelector: string, input?: Record<string, unknown>) => {
      dispatch({ type: "action", action: { kind: "start", target: workflowSelector } });
      try {
        return await api.startRun(workflowSelector, input);
      } finally {
        dispatch({ type: "action", action: undefined });
      }
    },
    [api],
  );

  const cancelRun = useCallback(
    async (runId: string) => {
      dispatch({ type: "action", action: { kind: "cancel", target: runId } });
      try {
        await api.cancelRun(runId);
      } finally {
        dispatch({ type: "action", action: undefined });
      }
    },
    [api],
  );

  return { state, reconcile, startRun, cancelRun, selectRun };
}
