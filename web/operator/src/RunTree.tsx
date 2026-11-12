import { useCallback } from "react";
import { CornerDownRight } from "lucide-react";

import type { OperatorApi, RunLink, RunLinks } from "./api";
import { type MachineReadRefresh, useRead } from "./MachineWorkspace";
import type { RunSummaryMsg } from "./model";
import { shortRunId } from "./RunListPanel";
import { RunStatus } from "./RunStatus";
import { formatAbsoluteTime, formatTimeAgo, useNow } from "./timeAgo";

/**
 * A run's native lineage: the runs whose `ava.call`/`ava.start_run` started it
 * and the runs it started. Re-read on each run change (a child appears when its
 * start step runs) and while the run is live.
 */
export function useRunLinks(
  api: OperatorApi,
  runId: string | undefined,
  refresh: MachineReadRefresh = {},
) {
  const load = useCallback(
    (signal: AbortSignal) => api.getRunLinks!(runId!, signal),
    [api, runId],
  );
  const read = useRead(api.getRunLinks && runId ? load : undefined, refresh);
  return { links: read.value, error: read.error };
}

/** The child run each start/call node of the shown graph created or attached to. */
export function childRunsByNode(
  links: RunLinks | undefined,
  runId: string | undefined,
  graph: Record<string, { children: string[] }> | undefined,
  nodeTypes: Record<string, string> | undefined,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const link of links?.children ?? []) {
    if (link.parentRunId !== runId) continue;
    result[link.nodeId] = link.childRunId;
    // `ava.call` is a start node and the wait right after it: both lead to the child.
    for (const next of graph?.[link.nodeId]?.children ?? [])
      if (nodeTypes?.[next] === "wait") result[next] = link.childRunId;
  }
  return result;
}

/** "support_ticket #611" — the run's flow or machine name and its key, if any. */
export function runName(summary: RunSummaryMsg | undefined, workflowName?: string) {
  return [workflowName, summary?.label].filter(Boolean).join(" ");
}

interface RunTreeProps {
  runId: string;
  links: RunLinks;
  runs: Record<string, RunSummaryMsg>;
  workflowName: (workflowId: string) => string | undefined;
  /** Opens another run's page (its flow or machine may differ from this one). */
  onOpenRun: (runId: string) => void;
}

/**
 * The selected run between its parents and its children, in the runs panel under
 * the run list. The same for flow and machine runs; rows are the run rows of the
 * list above (status, time ago with the exact time on hover) and never truncate.
 */
export function RunTree({ runId, links, runs, workflowName, onOpenRun }: RunTreeProps) {
  const now = useNow();
  const parents = links.parents.filter((link) => link.childRunId === runId);
  const children = links.children.filter((link) => link.parentRunId === runId);
  if (!parents.length && !children.length) return null;
  const row = (id: string, depth: number, relation: string, link?: RunLink) => {
    const summary = runs[id];
    const triggeredAt = summary?.triggeredAt ? new Date(summary.triggeredAt * 1000) : undefined;
    const name = runName(summary, summary ? workflowName(summary.workflowId) : undefined);
    const current = id === runId;
    const content = (
      <>
        <span
          className="run-tree-fields flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-secondary"
          style={{ paddingLeft: depth * 14 }}
        >
          {depth > 0 && (
            <CornerDownRight
              aria-hidden="true"
              className="size-3 shrink-0 text-run-status-muted"
            />
          )}
          <span className="text-[8px] tracking-[0.12em] text-run-status-muted uppercase">
            {relation}
          </span>
          {name && <span className="run-label text-ink [overflow-wrap:anywhere]">{name}</span>}
          <span className="run-short-id whitespace-nowrap" title={id}>
            {shortRunId(id)}
          </span>
          {triggeredAt && (
            <time
              className="run-triggered-at whitespace-nowrap"
              dateTime={triggeredAt.toISOString()}
              title={formatAbsoluteTime(triggeredAt)}
            >
              {formatTimeAgo(triggeredAt, now)}
            </time>
          )}
          {link && !link.created && <span className="whitespace-nowrap">· attached</span>}
        </span>
        <span className="justify-self-end text-right">
          {summary ? <RunStatus status={summary.status} /> : null}
        </span>
      </>
    );
    const className = `run-tree-row grid w-full grid-cols-[minmax(0,1fr)_100px] items-center gap-3 border-0 border-b border-run-list-line py-[7px] pl-[16px] pr-[30px] text-left text-[10px] leading-tight text-ink`;
    return (
      <li key={`${relation}:${id}`}>
        {current ? (
          <div
            className={`${className} bg-selected shadow-[inset_2px_0_var(--color-acid)]`}
            aria-current="page"
          >
            {content}
          </div>
        ) : (
          <button
            type="button"
            className={`${className} cursor-pointer bg-transparent hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid`}
            onClick={() => onOpenRun(id)}
            aria-label={`Open ${relation} run ${name ? `${name}, ` : ""}${id}${summary ? `, ${summary.status}` : ""}`}
          >
            {content}
          </button>
        )}
      </li>
    );
  };
  const depth = parents.length ? 1 : 0;
  return (
    <section aria-label="Run tree" className="run-tree shrink-0 border-t-2 border-line">
      <div className="run-list-columns grid h-7 grid-cols-[minmax(0,1fr)_100px] items-center gap-3 border-b border-run-list-line pl-[16px] pr-[30px] font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase">
        <span>Run tree</span>
        <span className="text-right">Status</span>
      </div>
      <ul className="m-0 max-h-48 list-none overflow-auto p-0">
        {parents.map((link) => row(link.parentRunId, 0, "parent", link))}
        {row(runId, depth, "this run")}
        {children.map((link) => row(link.childRunId, depth + 1, "child", link))}
      </ul>
    </section>
  );
}
