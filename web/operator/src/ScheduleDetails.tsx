import { useId, useMemo } from "react";

import { describeCron } from "./cron";
import type { FlowInfoMsg, RunSummaryMsg } from "./model";
import { compareNewestRun } from "./RunListPanel";

const SCHEDULE_TIMESTAMP_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

interface ScheduleDetailsProps {
  workflow: FlowInfoMsg;
  runs: Record<string, RunSummaryMsg>;
  onSelectRun?: (runId: string) => void;
}

function timestamp(seconds: number): Date | undefined {
  return seconds ? new Date(seconds * 1000) : undefined;
}

/** Cron state for one workflow; whether it is enabled lives only in operator memory. */
export function ScheduleDetails({ workflow, runs, onSelectRun }: ScheduleDetailsProps) {
  const headingId = useId();
  const lastScheduledRun = useMemo(
    () =>
      Object.values(runs)
        .filter(
          (run) => run.workflowId === workflow.workflowId && run.triggeredBy === "scheduled",
        )
        .sort(compareNewestRun)[0],
    [runs, workflow.workflowId],
  );
  const enabled = workflow.scheduleEnabled;
  const nextRun = enabled ? timestamp(workflow.nextRunAt) : undefined;
  const lastRun = timestamp(workflow.lastRunAt);

  return (
    <section
      className="schedule-details w-full overflow-hidden rounded border border-line-strong bg-panel text-[10px] shadow-[0_18px_50px_var(--color-shadow-1)]"
      aria-labelledby={headingId}
    >
      <header className="flex h-9 items-center justify-between border-b border-line px-[13px]">
        <h2 id={headingId} className="m-0 text-[11px] font-semibold text-ink">
          Schedule
        </h2>
        <span
          className={`schedule-state rounded-full px-2 py-0.5 font-medium ${enabled ? "bg-selected text-acid" : "bg-warning-surface text-warning-ink"}`}
        >
          {enabled ? "Enabled" : "Disabled"}
        </span>
      </header>
      <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 px-[13px] py-2 [&_dt]:text-secondary [&_dd]:m-0 [&_dd]:min-w-0 [&_dd]:truncate [&_dd]:text-ink">
        <dt>Runs</dt>
        <dd title={workflow.cron}>
          {describeCron(workflow.cron) ?? "Custom schedule"}{" "}
          <code className="font-mono text-[9px] text-secondary">{workflow.cron}</code>
        </dd>
        <dt>Input</dt>
        <dd title={workflow.scheduleInput && JSON.stringify(workflow.scheduleInput)}>
          {workflow.scheduleInput ? (
            <code className="font-mono text-[9px]">
              {JSON.stringify(workflow.scheduleInput)}
            </code>
          ) : (
            "Workflow defaults"
          )}
        </dd>
        <dt>Next run</dt>
        <dd>
          {nextRun ? (
            <time dateTime={nextRun.toISOString()}>
              {SCHEDULE_TIMESTAMP_FORMAT.format(nextRun)}
            </time>
          ) : enabled ? (
            "Not scheduled"
          ) : (
            "None while disabled"
          )}
        </dd>
        <dt>Last run</dt>
        <dd>
          {lastRun ? (
            <time dateTime={lastRun.toISOString()}>
              {SCHEDULE_TIMESTAMP_FORMAT.format(lastRun)}
            </time>
          ) : (
            "Not since the operator started"
          )}
          {lastScheduledRun && onSelectRun && (
            <button
              type="button"
              className="schedule-last-status ml-1.5 cursor-pointer border-0 bg-transparent p-0 capitalize text-acid underline underline-offset-2 hover:text-acid-hover"
              aria-label={`Open last scheduled run ${lastScheduledRun.runId}, ${lastScheduledRun.status}`}
              onClick={() => onSelectRun(lastScheduledRun.runId)}
            >
              {lastScheduledRun.status}
            </button>
          )}
        </dd>
      </dl>
      <p className="m-0 border-t border-line-subtle px-[13px] py-1.5 text-[9px] text-secondary">
        Schedules start disabled and reset when the operator restarts.
      </p>
    </section>
  );
}
