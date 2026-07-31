import { useCallback, useEffect, useReducer, useRef } from "react";

import type { OperatorApi, StructuralBaseline } from "./api";
import type {
  AgentEventDescriptorMsg,
  CatalogSnapshotMsg,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  RunSnapshotMsg,
} from "./generated/operator";

export interface OperatorProjection {
  catalog?: CatalogSnapshotMsg;
  runs: Record<string, RunSnapshotMsg>;
  liveEvents: Record<string, AgentEventDescriptorMsg[]>;
  liveLogs: Record<string, LogRecordDescriptorMsg[]>;
  operatorInstanceId: string;
  sequence: string;
  connection: "connecting" | "live" | "reconnecting";
  error?: string;
  action?: { kind: "start" | "cancel"; target: string };
}

type ProjectionAction =
  | { type: "baseline"; baseline: StructuralBaseline }
  | { type: "envelope"; envelope: OperatorUpdateEnvelope }
  | { type: "connection"; connection: OperatorProjection["connection"]; error?: string }
  | { type: "action"; action?: OperatorProjection["action"] };

export const emptyProjection: OperatorProjection = {
  runs: {},
  liveEvents: {},
  liveLogs: {},
  operatorInstanceId: "",
  sequence: "0",
  connection: "connecting",
};

export function projectionReducer(
  state: OperatorProjection,
  action: ProjectionAction,
): OperatorProjection {
  if (action.type === "baseline") {
    const runs = Object.fromEntries(
      action.baseline.runs
        .filter((run) => run.summary)
        .map((run) => [run.summary!.runId, run]),
    );
    return {
      ...emptyProjection,
      catalog: action.baseline.catalog,
      runs,
      operatorInstanceId: action.baseline.catalog.operatorInstanceId,
      sequence: action.baseline.asOfSequence,
      connection: "live",
    };
  }
  if (action.type === "connection") {
    return { ...state, connection: action.connection, error: action.error };
  }
  if (action.type === "action") return { ...state, action: action.action };

  const { envelope } = action;
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
  if (change.oneofKind === "catalogReplaced") {
    if (change.catalogReplaced.catalog) next.catalog = change.catalogReplaced.catalog;
    return next;
  }
  if (change.oneofKind === "runCreated" && change.runCreated.summary) {
    const summary = change.runCreated.summary;
    next.runs = {
      ...state.runs,
      [summary.runId]: {
        operatorInstanceId: state.operatorInstanceId,
        asOfSequence: update.sequence,
        summary,
        nodes: change.runCreated.nodes,
        latestLogSequence: "0",
        logPageToken: "",
        topology: change.runCreated.topology,
      },
    };
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
  const run = state.runs[runId];
  if (!run) throw new Error(`Operator update referenced unknown run ${runId}`);

  if (change.oneofKind === "runStatusChanged" && run.summary) {
    next.runs = {
      ...state.runs,
      [runId]: {
        ...run,
        summary: {
          ...run.summary,
          status: change.runStatusChanged.status,
          startedAt: change.runStatusChanged.startedAt,
          endedAt: change.runStatusChanged.endedAt,
          revision: change.runStatusChanged.revision,
        },
      },
    };
  } else if (change.oneofKind === "nodeStatusChanged") {
    const changed = change.nodeStatusChanged;
    next.runs = {
      ...state.runs,
      [runId]: {
        ...run,
        nodes: run.nodes.map((node) =>
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
      },
    };
  } else if (change.oneofKind === "logAppended" && change.logAppended.log) {
    next.liveLogs = {
      ...state.liveLogs,
      [runId]: [...(state.liveLogs[runId] ?? []), change.logAppended.log],
    };
  } else if (
    change.oneofKind === "agentEventAppended" &&
    change.agentEventAppended.event
  ) {
    const key = `${runId}:${change.agentEventAppended.nodeId}`;
    next.liveEvents = {
      ...state.liveEvents,
      [key]: [...(state.liveEvents[key] ?? []), change.agentEventAppended.event],
    };
  } else if (change.oneofKind === "traceFinalized" && change.traceFinalized.trace) {
    next.runs = {
      ...state.runs,
      [runId]: {
        ...run,
        nodes: run.nodes.map((node) =>
          node.nodeId === change.traceFinalized.nodeId
            ? { ...node, trace: change.traceFinalized.trace }
            : node,
        ),
      },
    };
  }
  return next;
}


export function useOperatorProjection(api: OperatorApi) {
  const [state, dispatch] = useReducer(projectionReducer, emptyProjection);
  const generation = useRef(0);

  const reconcile = useCallback(async () => {
    const baseline = await api.loadBaseline();
    dispatch({ type: "baseline", baseline });
    return baseline;
  }, [api]);

  useEffect(() => {
    const currentGeneration = ++generation.current;
    let stopped = false;
    const run = async () => {
      let retryMilliseconds = 250;
      while (!stopped && generation.current === currentGeneration) {
        try {
          dispatch({ type: "connection", connection: "connecting" });
          const baseline = await api.loadBaseline();
          if (stopped) return;
          dispatch({ type: "baseline", baseline });
          retryMilliseconds = 250;
          let sequence = baseline.asOfSequence;
          for await (const envelope of api.streamUpdates(
            baseline.catalog.operatorInstanceId,
            sequence,
          )) {
            if (stopped) return;
            if (envelope.payload.oneofKind !== "update") break;
            if (BigInt(envelope.payload.update.sequence) !== BigInt(sequence) + 1n) break;
            dispatch({ type: "envelope", envelope });
            sequence = envelope.payload.update.sequence;
          }
          dispatch({ type: "connection", connection: "reconnecting" });
        } catch (error) {
          if (stopped) return;
          dispatch({
            type: "connection",
            connection: "reconnecting",
            error: error instanceof Error ? error.message : "Operator connection failed",
          });
          const { promise, resolve } = Promise.withResolvers<void>();
          window.setTimeout(resolve, retryMilliseconds);
          await promise;
          retryMilliseconds = Math.min(retryMilliseconds * 2, 4000);
        }
      }
    };
    void run();
    return () => {
      stopped = true;
      generation.current += 1;
    };
  }, [api]);

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

  return { state, reconcile, startRun, cancelRun };
}
