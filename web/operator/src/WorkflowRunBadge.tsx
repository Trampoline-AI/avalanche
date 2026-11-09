import type { RunSummaryMsg } from "./model";
import { statusTone } from "./RunStatus";

export interface WorkflowRunCounts {
  total: number;
  success: number;
  error: number;
}

export const emptyRunCounts: WorkflowRunCounts = { total: 0, success: 0, error: 0 };

export function countWorkflowRuns(
  runs: Readonly<Record<string, RunSummaryMsg>>,
): Map<string, WorkflowRunCounts> {
  const counts = new Map<string, WorkflowRunCounts>();
  for (const run of Object.values(runs)) {
    let workflowCounts = counts.get(run.workflowId);
    if (!workflowCounts) {
      workflowCounts = { ...emptyRunCounts };
      counts.set(run.workflowId, workflowCounts);
    }
    workflowCounts.total += 1;
    const tone = statusTone(run.status);
    if (tone === "success") workflowCounts.success += 1;
    if (tone === "danger") workflowCounts.error += 1;
  }
  return counts;
}

export function runRingShares({ total, success, error }: WorkflowRunCounts) {
  if (total === 0) return { success: 0, error: 0, neutral: 1 };
  const errorShare = error > 0 ? Math.max(0.1, error / total) : 0;
  // Reserve the minimum error arc, then divide the remainder proportionally.
  const successShare = total === error ? 0 : (success / (total - error)) * (1 - errorShare);
  return { success: successShare, error: errorShare, neutral: 1 - errorShare - successShare };
}

export function WorkflowRunBadge({ counts }: { counts: WorkflowRunCounts }) {
  const shares = runRingShares(counts);
  const other = counts.total - counts.success - counts.error;
  const label = `${counts.total} ${counts.total === 1 ? "run" : "runs"}: ${counts.success} successful, ${counts.error} failed, ${other} other`;
  const errorEnd = shares.error * 100;
  const successEnd = (shares.error + shares.success) * 100;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className="workflow-run-badge absolute -top-3.5 -right-1 grid size-[22px] place-items-center rounded-full p-[2px] font-mono font-medium text-ink ring-2 ring-panel"
      style={{
        background: `conic-gradient(var(--color-run-status-danger) 0% ${errorEnd}%, var(--color-run-status-success) ${errorEnd}% ${successEnd}%, var(--color-line) ${successEnd}% 100%)`,
      }}
    >
      <span
        aria-hidden="true"
        className={`grid size-full place-items-center rounded-full bg-panel leading-none tabular-nums ${counts.total < 100 ? "text-[10px]" : counts.total < 1000 ? "text-[8px]" : "text-[6px]"}`}
      >
        {counts.total}
      </span>
    </span>
  );
}
