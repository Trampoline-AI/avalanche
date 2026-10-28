import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Maximize2, Minimize2, Workflow } from "lucide-react";

import { formatElapsedSeconds } from "./elapsedDuration";
import type { RunSummaryMsg } from "./model";
import { RunStatus } from "./RunStatus";
import { DeploymentList } from "./DeploymentList";
import { useTimelineScrollHandle } from "./useTimelineScrollHandle";
import type { WorkflowWorkspaceProps } from "./WorkflowWorkspace";

// Halve the centered vertical insets without changing the 10px/11px content
// or 1px divider: 44 - 16.5 = 27.5; 56 - 22 = 34.
const RUN_ROW_HEIGHT = 27.5;
const EXPANDED_RUN_ROW_HEIGHT = 34;
const RUN_ROW_OVERSCAN = 8;
const RUN_PAGE_SIZE = 25;
const statusChipClass = (active: boolean) =>
  `inline-flex h-7 cursor-pointer items-center gap-1.5 rounded border px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-acid ${active ? "border-acid bg-selected text-acid" : "border-transparent bg-panel text-ink hover:border-acid"}`;
const RUN_TIMESTAMP_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

interface RunListPanelProps extends Pick<
  WorkflowWorkspaceProps,
  "currentLabel" | "currentRowAction" | "deployments" | "timelineView" | "onTimelineViewChange"
> {
  workflowId: string;
  runs: Record<string, RunSummaryMsg>;
  selectedRunId?: string;
  onSelectRun: (runId: string | undefined) => void;
  onViewAll?: () => void;
  onClose?: () => void;
  expanded?: boolean;
}

export function compareNewestRun(left: RunSummaryMsg, right: RunSummaryMsg) {
  const leftSequence = BigInt(left.createdSequence);
  const rightSequence = BigInt(right.createdSequence);
  if (leftSequence === rightSequence) return left.runId.localeCompare(right.runId);
  return leftSequence < rightSequence ? 1 : -1;
}

function runElapsedSeconds(summary: RunSummaryMsg): number | undefined {
  if (!summary.startedAt || !summary.endedAt) return undefined;
  return Math.max(0, summary.endedAt - summary.startedAt);
}

function runDuration(elapsedSeconds: number | undefined) {
  return elapsedSeconds === undefined ? "—" : formatElapsedSeconds(elapsedSeconds);
}

function runTriggeredAt(summary: RunSummaryMsg): Date | undefined {
  return summary.triggeredAt ? new Date(summary.triggeredAt * 1000) : undefined;
}

export function RunListPanel({
  workflowId,
  runs,
  selectedRunId,
  onSelectRun,
  onViewAll,
  onClose,
  expanded = false,
  currentLabel,
  currentRowAction,
  deployments,
  timelineView,
  onTimelineViewChange,
}: RunListPanelProps) {
  const [internalView, setInternalView] = useState<"runs" | "deployments">("runs");
  const view = deployments ? (timelineView ?? internalView) : "runs";
  const scrollElement = useRef<HTMLDivElement>(null);
  const scrollHandle = useRef<HTMLDivElement>(null);
  const tabsId = useId();
  const tabButtons = useRef<Record<"runs" | "deployments", HTMLButtonElement | null>>({
    runs: null,
    deployments: null,
  });
  const selectView = (tab: "runs" | "deployments") => {
    if (timelineView === undefined) setInternalView(tab);
    onTimelineViewChange?.(tab);
  };
  const tabClass = (selected: boolean) =>
    `relative inline-flex h-full items-center border-0 bg-transparent p-0 font-mono text-[10px] tracking-[0.16em] uppercase after:absolute after:inset-x-0 after:h-0.5 ${expanded ? "after:-bottom-px" : "after:-bottom-0.5"} ${selected ? "font-semibold text-ink after:bg-acid" : "text-run-status-muted hover:text-ink"}`;
  const toggleButton = useRef<HTMLButtonElement>(null);
  const previousExpanded = useRef(expanded);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(0);
  const [timelinePageCount, setTimelinePageCount] = useState(1);
  const [loadingPage, setLoadingPage] = useState(false);
  const rowHeight = expanded ? EXPANDED_RUN_ROW_HEIGHT : RUN_ROW_HEIGHT;
  const rowPadding = expanded ? 11 : 8.25;
  // Current's unchanged line boxes are 15px compact / 16px expanded.
  const currentHeight = expanded ? 36.5 : 30;
  const currentPadding = expanded ? 9.75 : 7;
  const workflowRuns = useMemo(
    () =>
      Object.values(runs)
        .filter((summary) => summary.workflowId === workflowId)
        .sort(compareNewestRun),
    [runs, workflowId],
  );
  const newestRunId = workflowRuns[0]?.runId;
  const statuses = useMemo(() => {
    const counts = new Map<string, number>();
    for (const run of workflowRuns) {
      if (run.status) counts.set(run.status, (counts.get(run.status) ?? 0) + 1);
    }
    // Keep an active filter clearable when live history no longer contains it.
    if (status && !counts.has(status)) counts.set(status, 0);
    return [...counts.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [status, workflowRuns]);
  const filteredRuns = useMemo(() => {
    if (!expanded) return workflowRuns;
    const query = search.trim().toLowerCase();
    const from = fromDate ? new Date(`${fromDate}T00:00:00`).getTime() : -Infinity;
    const until = toDate ? new Date(`${toDate}T23:59:59.999`).getTime() : Infinity;
    return workflowRuns.filter((run) => {
      if (status && run.status !== status) return false;
      if (query && !run.runId.toLowerCase().includes(query)) return false;
      if (!fromDate && !toDate) return true;
      const triggeredAt = runTriggeredAt(run)?.getTime();
      return triggeredAt !== undefined && triggeredAt >= from && triggeredAt <= until;
    });
  }, [expanded, fromDate, search, status, toDate, workflowRuns]);
  const pageCount = Math.max(1, Math.ceil(filteredRuns.length / RUN_PAGE_SIZE));
  const pageIndex = Math.min(page, pageCount - 1);
  if (page !== pageIndex) setPage(pageIndex);
  const pageOffset = expanded ? pageIndex * RUN_PAGE_SIZE : 0;
  const visibleRunCount = expanded
    ? Math.min(RUN_PAGE_SIZE, filteredRuns.length - pageOffset)
    : Math.min(timelinePageCount * RUN_PAGE_SIZE, filteredRuns.length);
  useEffect(() => {
    setTimelinePageCount(1);
  }, [workflowId]);
  useEffect(() => {
    setLoadingPage(false);
    const viewport = scrollElement.current;
    if (view !== "runs" || expanded || !viewport || visibleRunCount >= workflowRuns.length)
      return;
    let frame: number | undefined;
    const loadNearBottom = () => {
      if (
        frame !== undefined ||
        visibleRunCount * rowHeight - viewport.scrollTop - viewport.clientHeight > rowHeight * 4
      )
        return;
      // Expanded history already pages the hydrated runs prop. Reveal the next
      // same-sized page on the next frame; never introduce a second API path.
      setLoadingPage(true);
      frame = requestAnimationFrame(() => {
        setTimelinePageCount((count) => count + 1);
        setLoadingPage(false);
      });
    };
    viewport.addEventListener("scroll", loadNearBottom, { passive: true });
    loadNearBottom();
    return () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      viewport.removeEventListener("scroll", loadNearBottom);
    };
  }, [view, expanded, rowHeight, visibleRunCount, workflowId, workflowRuns.length]);
  useEffect(() => {
    if (scrollElement.current) scrollElement.current.scrollTop = 0;
  }, [workflowId, search, status, fromDate, toDate, pageIndex]);
  useEffect(() => {
    if (!expanded && selectedRunId === newestRunId && scrollElement.current) {
      scrollElement.current.scrollTop = 0;
    }
  }, [expanded, selectedRunId, newestRunId]);
  useEffect(() => {
    if (previousExpanded.current !== expanded) toggleButton.current?.focus();
    previousExpanded.current = expanded;
  }, [expanded]);
  const virtualizer = useVirtualizer({
    count: visibleRunCount,
    getScrollElement: () => scrollElement.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) => `run:${filteredRuns[pageOffset + index].runId}`,
    overscan: RUN_ROW_OVERSCAN,
  });
  useEffect(() => {
    virtualizer.measure();
  }, [view, rowHeight, virtualizer]);
  useTimelineScrollHandle(scrollElement, scrollHandle, expanded, view === "runs");

  return (
    <section
      className={
        expanded
          ? "all-runs-panel flex h-full min-h-0 min-w-0 flex-col overflow-hidden border-r border-line bg-panel"
          : "run-list-panel flex h-full w-full flex-col overflow-hidden rounded border border-line bg-overlay-panel shadow-[0_6px_20px_var(--color-shadow-2)]"
      }
      aria-label="Timeline"
      onKeyDown={(event) => {
        if (expanded && event.key === "Escape") {
          event.stopPropagation();
          onClose?.();
        }
      }}
    >
      <header
        className={`flex shrink-0 items-center justify-between ${expanded ? "h-14 border-b border-line-subtle px-[20px]" : "h-9 border-b-2 border-line px-[9px]"}`}
      >
        <div className="flex min-w-0 self-stretch items-center">
          {deployments ? (
            <div
              role="tablist"
              aria-label="Timeline view"
              className="flex h-full items-center gap-4"
            >
              {(["runs", "deployments"] as const).map((tab) => (
                <button
                  key={tab}
                  ref={(button) => {
                    tabButtons.current[tab] = button;
                  }}
                  id={`${tabsId}-${tab}`}
                  type="button"
                  role="tab"
                  aria-selected={view === tab}
                  aria-controls={`${tabsId}-panel`}
                  tabIndex={view === tab ? 0 : -1}
                  onClick={() => selectView(tab)}
                  onKeyDown={(event) => {
                    let next: "runs" | "deployments";
                    if (event.key === "Home") next = "runs";
                    else if (event.key === "End") next = "deployments";
                    else if (event.key === "ArrowLeft" || event.key === "ArrowRight")
                      next = tab === "runs" ? "deployments" : "runs";
                    else return;
                    event.preventDefault();
                    tabButtons.current[next]?.focus();
                    selectView(next);
                  }}
                  className={`${tabClass(view === tab)} cursor-pointer focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-acid`}
                >
                  {tab === "runs" ? "Runs" : "Deployments"}
                </button>
              ))}
            </div>
          ) : (
            <span className={tabClass(true)}>Runs</span>
          )}
        </div>
        <div className={`flex items-center ${expanded ? "gap-3" : "gap-2"}`}>
          {!expanded && onViewAll && (
            <button
              ref={toggleButton}
              type="button"
              aria-label="Expand timeline"
              title="Expand timeline"
              onClick={onViewAll}
              className="grid size-5 cursor-pointer place-items-center rounded border-0 bg-transparent text-secondary hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
            >
              <Maximize2 aria-hidden="true" className="size-3" />
            </button>
          )}
          {expanded && (
            <button
              ref={toggleButton}
              type="button"
              aria-label="Collapse timeline"
              title="Collapse timeline"
              onClick={onClose}
              className="grid size-8 cursor-pointer place-items-center rounded border-0 bg-transparent text-lg text-secondary hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
            >
              <Minimize2 aria-hidden="true" className="size-4" />
            </button>
          )}
        </div>
      </header>
      <div
        id={`${tabsId}-panel`}
        role={deployments ? "tabpanel" : undefined}
        aria-labelledby={deployments ? `${tabsId}-${view}` : undefined}
        className="flex min-h-0 flex-1 flex-col"
      >
        {expanded && view === "runs" && (
          <div className="all-runs-filters flex shrink-0 flex-col gap-[20px] border-b-2 border-line p-[20px] text-xs text-secondary">
            <div
              role="group"
              aria-label="Filter by status"
              className="flex flex-wrap items-center gap-2"
            >
              <span className="mr-1 font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase">
                Status
              </span>
              <button
                type="button"
                className={statusChipClass(!status)}
                aria-pressed={!status}
                onClick={() => {
                  setStatus("");
                  setPage(0);
                }}
              >
                All <span className="font-mono text-secondary">{workflowRuns.length}</span>
              </button>
              {statuses.map(([value, count]) => (
                <button
                  key={value}
                  type="button"
                  className={statusChipClass(status === value)}
                  aria-pressed={status === value}
                  onClick={() => {
                    setStatus(status === value ? "" : value);
                    setPage(0);
                  }}
                >
                  {value} <span className="font-mono text-secondary">{count}</span>
                </button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-[12px]">
              <label className="flex min-w-0 flex-col gap-[8px] font-medium">
                From
                <input
                  type="date"
                  value={fromDate}
                  onChange={(event) => {
                    setFromDate(event.target.value);
                    setPage(0);
                  }}
                  className="h-9 min-w-0 rounded border border-line bg-panel px-3 py-2 text-xs font-normal text-ink outline-acid"
                />
              </label>
              <label className="flex min-w-0 flex-col gap-[8px] font-medium">
                To
                <input
                  type="date"
                  value={toDate}
                  onChange={(event) => {
                    setToDate(event.target.value);
                    setPage(0);
                  }}
                  className="h-9 min-w-0 rounded border border-line bg-panel px-3 py-2 text-xs font-normal text-ink outline-acid"
                />
              </label>
            </div>
            <details>
              <summary className="cursor-pointer font-medium text-ink focus-visible:outline-2 focus-visible:outline-acid">
                Advanced filters{search.trim() ? " (1 active)" : ""}
              </summary>
              <label className="mt-4 flex flex-col gap-[8px] font-medium">
                Run ID
                <input
                  type="search"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setPage(0);
                  }}
                  placeholder="Search by run ID"
                  className="h-9 min-w-0 rounded border border-line bg-panel px-3 py-2 text-xs font-normal text-ink outline-acid"
                />
              </label>
            </details>
          </div>
        )}
        {view === "runs" && (
          <div
            className={`flex shrink-0 items-center shadow-[inset_0_-1px_var(--color-run-list-line)] ${selectedRunId === undefined ? "bg-selected" : "bg-transparent"}`}
          >
            <button
              type="button"
              aria-pressed={selectedRunId === undefined}
              onClick={() => onSelectRun(undefined)}
              style={{
                height: currentHeight,
                paddingTop: currentPadding,
                paddingBottom: currentPadding,
              }}
              className={`run-list-current flex min-w-0 flex-1 cursor-pointer items-center gap-2 border-0 border-b border-run-list-line text-left font-mono font-medium text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid ${expanded ? "px-[20px] text-xs" : "pl-[9px] pr-[17px] text-[10px]"} ${selectedRunId === undefined ? "active bg-selected shadow-[inset_2px_0_var(--color-acid)] hover:bg-selected-hover" : "bg-transparent hover:bg-hover"}`}
            >
              <Workflow aria-hidden="true" className="size-3.5 shrink-0 text-secondary" />
              <span className="flex-1 truncate">{currentLabel ?? "Current definition"}</span>
            </button>
            {currentRowAction && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  currentRowAction.onClick();
                }}
                className={`shrink-0 cursor-pointer rounded border-0 bg-transparent text-[10px] text-secondary hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid ${expanded ? "mr-[20px]" : "mr-[17px]"}`}
              >
                {currentRowAction.label}
              </button>
            )}
          </div>
        )}
        {view === "deployments" && deployments ? (
          <DeploymentList deployments={deployments} expanded={expanded} />
        ) : (
          <>
            <div
              className={`run-list-columns grid shrink-0 grid-cols-[minmax(0,1fr)_100px] items-center gap-3 border-b border-run-list-line font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase ${expanded ? "h-9 px-[20px]" : "h-7 px-[9px]"}`}
              aria-hidden="true"
            >
              <span>Run</span>
              <span>Status</span>
            </div>
            <div
              className={`timeline-scroll-shell relative min-h-0 ${expanded ? "flex-1" : ""}`}
            >
              <div
                className={
                  expanded
                    ? "run-list-scroll timeline-scroll h-full overflow-auto overscroll-contain"
                    : "run-list-scroll timeline-scroll max-h-48 overflow-auto overscroll-contain"
                }
                ref={scrollElement}
              >
                <div
                  className="run-list-virtual relative w-full"
                  style={{ height: virtualizer.getTotalSize() }}
                >
                  {virtualizer.getVirtualItems().map((virtualRow) => {
                    const rowStyle = {
                      height: virtualRow.size,
                      paddingTop: rowPadding,
                      paddingBottom: rowPadding,
                      transform: `translateY(${virtualRow.start}px)`,
                    };
                    const summary = filteredRuns[pageOffset + virtualRow.index];
                    const triggeredAt = summary.startedAt
                      ? new Date(summary.startedAt * 1000)
                      : runTriggeredAt(summary);
                    const elapsedSeconds = runElapsedSeconds(summary);
                    return (
                      <button
                        type="button"
                        className={`run-list-row run-list-columns absolute top-0 left-0 grid w-full cursor-pointer grid-cols-[minmax(0,1fr)_100px] items-center gap-3 border-0 border-b border-run-list-line text-left font-mono text-ink leading-none ${expanded ? "px-[20px] text-[11px]" : "px-[9px] text-[10px]"} ${selectedRunId === summary.runId ? "active bg-selected shadow-[inset_2px_0_var(--color-acid)] hover:bg-selected-hover" : "bg-transparent hover:bg-hover"}`}
                        key={summary.runId}
                        onClick={() => onSelectRun(summary.runId)}
                        aria-pressed={selectedRunId === summary.runId}
                        style={rowStyle}
                        title={summary.runId}
                        aria-label={`${summary.runId}, ${summary.status}, ${runDuration(elapsedSeconds)}, ${
                          triggeredAt
                            ? RUN_TIMESTAMP_FORMAT.format(triggeredAt)
                            : "start time not recorded"
                        }`}
                      >
                        <span className="min-w-0 truncate font-mono text-secondary">
                          <time
                            className="run-triggered-at"
                            dateTime={triggeredAt?.toISOString()}
                          >
                            {triggeredAt
                              ? RUN_TIMESTAMP_FORMAT.format(triggeredAt)
                              : "Start time not recorded"}
                          </time>
                          {" · "}
                          <span
                            className="run-duration"
                            title={
                              elapsedSeconds === undefined
                                ? undefined
                                : `${elapsedSeconds.toFixed(1)} seconds`
                            }
                          >
                            {runDuration(elapsedSeconds)}
                          </span>
                        </span>
                        <RunStatus status={summary.status} />
                      </button>
                    );
                  })}
                </div>
                {loadingPage && !expanded && (
                  <div
                    role="status"
                    className="run-list-loading px-[9px] py-1 text-[10px] text-secondary"
                  >
                    Loading runs…
                  </div>
                )}
                {filteredRuns.length === 0 && (
                  <span
                    className={`run-list-empty block font-mono text-secondary ${expanded ? "min-h-0 flex-1 overflow-auto px-[20px] py-[24px] text-xs" : "p-2.5 text-[8px]"}`}
                  >
                    {workflowRuns.length ? "No matching runs" : "No runs"}
                  </span>
                )}
              </div>
              <div ref={scrollHandle} className="timeline-scroll-handle" aria-hidden="true" />
            </div>
            {expanded && view === "runs" && (
              <nav
                aria-label="Run pagination"
                className="flex shrink-0 flex-col gap-3 border-t border-line-subtle px-[20px] py-[16px] text-xs text-secondary"
              >
                <span role="status">
                  {filteredRuns.length
                    ? `${pageOffset + 1}–${pageOffset + visibleRunCount} of ${filteredRuns.length} runs`
                    : "0 runs"}
                </span>
                <div className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    aria-label="Previous page"
                    disabled={pageIndex === 0}
                    onClick={() => setPage(pageIndex - 1)}
                    className="cursor-pointer rounded border border-line bg-transparent px-3 py-2 text-ink hover:bg-canvas focus-visible:outline-2 focus-visible:outline-acid disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Previous
                  </button>
                  <span>
                    Page {pageIndex + 1} of {pageCount}
                  </span>
                  <button
                    type="button"
                    aria-label="Next page"
                    disabled={pageIndex === pageCount - 1}
                    onClick={() => setPage(pageIndex + 1)}
                    className="cursor-pointer rounded border border-line bg-transparent px-3 py-2 text-ink hover:bg-canvas focus-visible:outline-2 focus-visible:outline-acid disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
              </nav>
            )}
          </>
        )}
      </div>
    </section>
  );
}
