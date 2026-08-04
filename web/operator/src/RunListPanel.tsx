import { useVirtualizer } from "@tanstack/react-virtual";
import { useMemo, useRef } from "react";

import type { RunSummaryMsg } from "./generated/operator";

const RUN_ROW_HEIGHT = 32;
const RUN_ROW_OVERSCAN = 8;

interface RunListPanelProps {
  workflowId: string;
  runs: Record<string, RunSummaryMsg>;
  selectedRunId?: string;
  onSelectRun: (runId: string) => void;
}

function compareNewestRun(left: RunSummaryMsg, right: RunSummaryMsg) {
  const leftSequence = BigInt(left.createdSequence);
  const rightSequence = BigInt(right.createdSequence);
  if (leftSequence === rightSequence) return left.runId.localeCompare(right.runId);
  return leftSequence < rightSequence ? 1 : -1;
}

function runDuration(summary: RunSummaryMsg) {
  if (!summary.startedAt || !summary.endedAt) return "—";
  return `${Math.max(0, summary.endedAt - summary.startedAt).toFixed(1)}s`;
}

export function RunListPanel({
  workflowId,
  runs,
  selectedRunId,
  onSelectRun,
}: RunListPanelProps) {
  const scrollElement = useRef<HTMLDivElement>(null);
  const workflowRuns = useMemo(
    () =>
      Object.values(runs)
        .filter((summary) => summary.workflowId === workflowId)
        .sort(compareNewestRun),
    [runs, workflowId],
  );
  const virtualizer = useVirtualizer({
    count: workflowRuns.length,
    getScrollElement: () => scrollElement.current,
    estimateSize: () => RUN_ROW_HEIGHT,
    getItemKey: (index) => workflowRuns[index].runId,
    overscan: RUN_ROW_OVERSCAN,
  });

  return (
    <section className="run-list-panel w-[300px] overflow-hidden rounded-[9px] border border-line bg-[rgba(255,255,255,.96)] shadow-[0_6px_20px_rgba(20,31,26,.1)]" aria-label="Workflow runs">
      <header className="flex h-[30px] items-center justify-between border-b border-line px-[9px] [&_strong]:text-[10px] [&_span]:font-mono [&_span]:text-[8px] [&_span]:text-secondary">
        <strong>Runs</strong>
        <span>{workflowRuns.length}</span>
      </header>
      {workflowRuns.length ? (
        <div className="run-list-scroll max-h-48 overflow-auto" ref={scrollElement}>
          <div className="run-list-virtual relative w-full" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const summary = workflowRuns[virtualRow.index];
              return (
                <button
                  type="button"
                  className={`run-list-row absolute top-0 left-0 grid w-full cursor-pointer grid-cols-[8px_minmax(0,1fr)_auto_38px] items-center gap-[7px] border-0 border-b border-[#eef1ef] bg-transparent px-[9px] text-left text-ink hover:bg-[#f4f6f5] [&_code]:overflow-hidden [&_code]:text-ellipsis [&_code]:whitespace-nowrap [&_code]:text-[9px] [&_code]:text-[#36423c] [&_time]:text-right [&_time]:font-mono [&_time]:text-[8px] [&_time]:text-secondary ${selectedRunId === summary.runId ? "active bg-[#edf3ff] shadow-[inset_2px_0_#2563eb]" : ""}`}
                  key={summary.runId}
                  onClick={() => onSelectRun(summary.runId)}
                  style={{
                    height: virtualRow.size,
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                  aria-label={`${summary.runId}, ${summary.status}, ${runDuration(summary)}`}
                >
                  <span className={`run-status-dot size-[7px] rounded-full ${summary.status === "success" ? "status-success bg-mint" : summary.status === "failed" ? "status-failed bg-danger" : summary.status === "running" ? "status-running bg-acid" : "bg-muted"}`} aria-hidden="true" />
                  <code title={summary.runId}>{summary.runId}</code>
                  <span className={`run-status-text text-[8px] capitalize ${summary.status === "success" ? "status-success text-mint" : summary.status === "failed" ? "status-failed text-danger" : summary.status === "running" ? "status-running text-acid" : ""}`}>
                    {summary.status}
                  </span>
                  <time>{runDuration(summary)}</time>
                </button>
              );
            })}
          </div>
        </div>
      ) : (
        <span className="run-list-empty block p-2.5 font-mono text-[8px] text-secondary">No runs yet</span>
      )}
    </section>
  );
}
