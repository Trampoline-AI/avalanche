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
    <section className="run-list-panel" aria-label="Workflow runs">
      <header>
        <strong>Runs</strong>
        <span>{workflowRuns.length}</span>
      </header>
      {workflowRuns.length ? (
        <div className="run-list-scroll" ref={scrollElement}>
          <div className="run-list-virtual" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const summary = workflowRuns[virtualRow.index];
              return (
                <button
                  type="button"
                  className={`run-list-row ${selectedRunId === summary.runId ? "active" : ""}`}
                  key={summary.runId}
                  onClick={() => onSelectRun(summary.runId)}
                  style={{
                    height: virtualRow.size,
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                  aria-label={`${summary.runId}, ${summary.status}, ${runDuration(summary)}`}
                >
                  <span className={`run-status-dot status-${summary.status}`} aria-hidden="true" />
                  <code title={summary.runId}>{summary.runId}</code>
                  <span className={`run-status-text status-${summary.status}`}>
                    {summary.status}
                  </span>
                  <time>{runDuration(summary)}</time>
                </button>
              );
            })}
          </div>
        </div>
      ) : (
        <span className="run-list-empty">No runs yet</span>
      )}
    </section>
  );
}
