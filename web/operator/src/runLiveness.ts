import type { OperatorApi } from "./api";

/**
 * The local operator's live run statuses: a run in one of them is still changing
 * (and can be cancelled); any other status is settled. Hosts with their own run
 * vocabulary declare theirs with `OperatorApi.liveRunStatuses`.
 */
export const LOCAL_LIVE_RUN_STATUSES: readonly string[] = [
  "requesting",
  "pending",
  "running",
  // A machine run parks between visits; cancelling it closes the instance.
  "waiting",
  "interrupted",
  "needs_attention",
];

/** The local operator's terminal run outcomes (`OperatorApi.terminalRunStatuses`). */
export const LOCAL_TERMINAL_RUN_STATUSES: readonly string[] = [
  "success",
  "failed",
  "cancelled",
];

/** Whether a run with this status is a terminal outcome on the host behind `api`. */
export function runStatusTerminal(
  api: Pick<OperatorApi, "terminalRunStatuses"> | undefined,
  status: string | undefined,
): boolean {
  if (status === undefined) return false;
  return (api?.terminalRunStatuses ?? LOCAL_TERMINAL_RUN_STATUSES).includes(status);
}

/** Whether a run with this status is live on the host behind `api`. */
export function runStatusLive(
  api: Pick<OperatorApi, "liveRunStatuses"> | undefined,
  status: string | undefined,
): boolean {
  if (status === undefined) return false;
  return (api?.liveRunStatuses ?? LOCAL_LIVE_RUN_STATUSES).includes(status);
}
