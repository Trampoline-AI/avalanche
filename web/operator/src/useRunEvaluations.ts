import { useEffect, useState } from "react";

import type { OperatorApi } from "./api";
import type { EvaluationRecord } from "./evaluations";

export interface RunEvaluationState {
  records: Record<string, EvaluationRecord>;
  loading: boolean;
  error?: string;
}

export const EMPTY_RUN_EVALUATIONS: RunEvaluationState = { records: {}, loading: false };
const LOADING_RUN_EVALUATIONS: RunEvaluationState = { records: {}, loading: true };
const POLL_INTERVAL_MS = 1500;

export function useRunEvaluations(
  api: OperatorApi,
  operatorInstanceId: string,
  runId: string | undefined,
): RunEvaluationState {
  const scope = `${operatorInstanceId}\0${runId ?? ""}`;
  const [state, setState] = useState<
    RunEvaluationState & { api: OperatorApi; scope: string }
  >();
  const current = state?.api === api && state.scope === scope ? state : undefined;

  useEffect(() => {
    if (runId === undefined) return;
    const selectedRunId = runId;
    const controller = new AbortController();
    let timer: number | undefined;
    async function refresh() {
      try {
        const evaluations = await api.listRunEvaluations(selectedRunId, controller.signal);
        if (!controller.signal.aborted)
          setState({
            api,
            scope,
            loading: false,
            records: Object.fromEntries(evaluations.map((record) => [record.nodeId, record])),
          });
      } catch (error: unknown) {
        if (!controller.signal.aborted)
          setState((previous) => ({
            api,
            scope,
            loading: false,
            records: previous?.api === api && previous.scope === scope ? previous.records : {},
            error: error instanceof Error ? error.message : "Unable to load evaluations",
          }));
      } finally {
        // Evaluations can complete after the workflow has reached terminal status.
        if (!controller.signal.aborted)
          timer = window.setTimeout(() => void refresh(), POLL_INTERVAL_MS);
      }
    }
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, scope, runId]);

  return runId === undefined ? EMPTY_RUN_EVALUATIONS : (current ?? LOADING_RUN_EVALUATIONS);
}
