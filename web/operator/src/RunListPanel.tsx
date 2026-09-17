import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronRight, Maximize2, Minimize2, Workflow } from "lucide-react";

import type { RunSummaryMsg } from "./model";

const RUN_ROW_HEIGHT = 32;
const RUN_ROW_OVERSCAN = 8;
const RUN_PAGE_SIZE = 25;
const TIMELINE_RUN_LIMIT = 20;
const RUN_TIMESTAMP_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "short",
  timeStyle: "short",
});

interface RunListPanelProps {
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

function runDuration(summary: RunSummaryMsg) {
  if (!summary.startedAt || !summary.endedAt) return "—";
  return `${Math.max(0, summary.endedAt - summary.startedAt).toFixed(1)}s`;
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
}: RunListPanelProps) {
  const scrollElement = useRef<HTMLDivElement>(null);
  const scrollHandle = useRef<HTMLDivElement>(null);
  const headingId = useId();
  const toggleButton = useRef<HTMLButtonElement>(null);
  const previousExpanded = useRef(expanded);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(0);
  const rowHeight = expanded ? 56 : RUN_ROW_HEIGHT;
  const workflowRuns = useMemo(
    () =>
      Object.values(runs)
        .filter((summary) => summary.workflowId === workflowId)
        .sort(compareNewestRun),
    [runs, workflowId],
  );
  const newestRunId = workflowRuns[0]?.runId;
  const statuses = useMemo(() => {
    const values = new Set<string>();
    for (const run of workflowRuns) {
      if (run.status) values.add(run.status);
    }
    if (status) values.add(status);
    return [...values].sort();
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
    : Math.min(TIMELINE_RUN_LIMIT, filteredRuns.length);
  const showTimelineFooter =
    !expanded && workflowRuns.length >= TIMELINE_RUN_LIMIT && Boolean(onViewAll);
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
    count: visibleRunCount + 1 + Number(showTimelineFooter),
    getScrollElement: () => scrollElement.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) =>
      index === 0
        ? "current"
        : index > visibleRunCount
          ? "view-all"
          : `run:${filteredRuns[pageOffset + index - 1].runId}`,
    overscan: RUN_ROW_OVERSCAN,
  });
  useEffect(() => {
    virtualizer.measure();
  }, [rowHeight, virtualizer]);
  useEffect(() => {
    const viewport = scrollElement.current;
    const handle = scrollHandle.current;
    if (!viewport || !handle) return;
    let drag: { pointerId: number; y: number; scrollTop: number } | undefined;
    const update = () => {
      const { clientHeight, scrollHeight, scrollTop } = viewport;
      const trackHeight = Math.max(0, clientHeight - 8);
      const height = Math.min(
        trackHeight,
        Math.max(24, (clientHeight / scrollHeight) * trackHeight),
      );
      const overflow = scrollHeight - clientHeight;
      handle.hidden = overflow <= 0;
      handle.style.height = `${height}px`;
      handle.style.transform = `translateY(${overflow > 0 ? (scrollTop / overflow) * (trackHeight - height) : 0}px)`;
    };
    const startDrag = (event: PointerEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag = { pointerId: event.pointerId, y: event.clientY, scrollTop: viewport.scrollTop };
      handle.setPointerCapture(event.pointerId);
      handle.dataset.dragging = "true";
    };
    const moveDrag = (event: PointerEvent) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const travel = viewport.clientHeight - 8 - handle.clientHeight;
      if (travel <= 0) return;
      viewport.scrollTop =
        drag.scrollTop +
        ((event.clientY - drag.y) / travel) * (viewport.scrollHeight - viewport.clientHeight);
    };
    const endDrag = () => {
      drag = undefined;
      delete handle.dataset.dragging;
    };
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    observer.observe(viewport.firstElementChild!);
    viewport.addEventListener("scroll", update, { passive: true });
    handle.addEventListener("pointerdown", startDrag);
    handle.addEventListener("pointermove", moveDrag);
    handle.addEventListener("lostpointercapture", endDrag);
    update();
    return () => {
      observer.disconnect();
      viewport.removeEventListener("scroll", update);
      handle.removeEventListener("pointerdown", startDrag);
      handle.removeEventListener("pointermove", moveDrag);
      handle.removeEventListener("lostpointercapture", endDrag);
    };
  }, [expanded]);

  return (
    <section
      className={
        expanded
          ? "all-runs-panel flex h-full min-h-0 min-w-0 flex-col overflow-hidden border-r border-line bg-panel"
          : "run-list-panel h-full w-full overflow-hidden rounded-[9px] border border-line bg-[rgba(255,255,255,.96)] shadow-[0_6px_20px_rgba(20,31,26,.1)]"
      }
      aria-labelledby={headingId}
      onKeyDown={(event) => {
        if (expanded && event.key === "Escape") {
          event.stopPropagation();
          onClose?.();
        }
      }}
    >
      <header
        className={`flex shrink-0 items-center justify-between ${expanded ? "h-14 border-b border-[#eef1ef] px-[20px]" : "h-9 border-b-2 border-line px-[9px]"}`}
      >
        <h2
          id={headingId}
          className={`m-0 font-semibold tracking-[-0.01em] text-ink ${expanded ? "text-sm" : "text-xs"}`}
        >
          Timeline
        </h2>
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
              className="grid size-8 cursor-pointer place-items-center rounded-md border-0 bg-transparent text-lg text-secondary hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
            >
              <Minimize2 aria-hidden="true" className="size-4" />
            </button>
          )}
        </div>
      </header>
      {expanded && (
        <div className="all-runs-filters flex shrink-0 flex-col gap-[20px] border-b-2 border-line p-[20px] text-xs text-secondary">
          <label className="flex flex-col gap-[8px] font-medium">
            Status
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                setPage(0);
              }}
              className="h-9 min-w-0 rounded-md border border-line bg-white px-3 py-2 text-xs font-normal text-ink outline-acid"
            >
              <option value="">All statuses</option>
              {statuses.map((value) => (
                <option key={value} value={value}>
                  {value.charAt(0).toUpperCase() + value.slice(1)}
                </option>
              ))}
            </select>
          </label>
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
                className="h-9 min-w-0 rounded-md border border-line bg-white px-3 py-2 text-xs font-normal text-ink outline-acid"
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
                className="h-9 min-w-0 rounded-md border border-line bg-white px-3 py-2 text-xs font-normal text-ink outline-acid"
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
                className="h-9 min-w-0 rounded-md border border-line bg-white px-3 py-2 text-xs font-normal text-ink outline-acid"
              />
            </label>
          </details>
        </div>
      )}
      <div className={`timeline-scroll-shell relative ${expanded ? "min-h-0 flex-1" : ""}`}>
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
                transform: `translateY(${virtualRow.start}px)`,
              };
              if (virtualRow.index === 0) {
                return (
                  <button
                    key="current"
                    type="button"
                    aria-pressed={selectedRunId === undefined}
                    onClick={() => onSelectRun(undefined)}
                    style={rowStyle}
                    className={`absolute top-0 left-0 flex w-full cursor-pointer items-center gap-2 border-0 border-b border-line text-left font-medium text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid ${expanded ? "px-[20px] text-xs" : "pl-[9px] pr-[17px] text-[10px]"} ${selectedRunId === undefined ? "active bg-[#edf3ff] shadow-[inset_2px_0_#2563eb] hover:bg-[#e4edff]" : "bg-transparent hover:bg-[#f4f6f5]"}`}
                  >
                    <Workflow aria-hidden="true" className="size-3.5 shrink-0 text-secondary" />
                    <span className="flex-1">Current</span>
                    <ChevronRight
                      aria-hidden="true"
                      className="size-3 shrink-0 text-secondary"
                    />
                  </button>
                );
              }
              if (virtualRow.index > visibleRunCount) {
                return (
                  <button
                    key="view-all"
                    type="button"
                    onClick={onViewAll}
                    style={rowStyle}
                    className="absolute top-0 left-0 flex w-full cursor-pointer items-center gap-1.5 border-0 bg-transparent pl-[9px] pr-[17px] text-left text-[10px] font-medium text-acid hover:bg-canvas focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid"
                  >
                    View all
                    <Maximize2 aria-hidden="true" className="size-3 shrink-0" />
                  </button>
                );
              }
              const summary = filteredRuns[pageOffset + virtualRow.index - 1];
              const triggeredAt = runTriggeredAt(summary);
              return (
                <button
                  type="button"
                  className={`run-list-row absolute top-0 left-0 grid w-full cursor-pointer grid-cols-[8px_minmax(0,1fr)_auto_38px] items-center border-0 border-b border-[#eef1ef] text-left text-ink leading-none [&_code]:truncate [&_code]:text-[#36423c] ${expanded ? "grid-rows-[16px_14px] gap-x-[10px] gap-y-[4px] px-[20px] py-[11px] [&_code]:text-[11px]" : "grid-rows-[12px_12px] gap-x-[7px] gap-y-0 pl-[9px] pr-[17px] py-1 [&_code]:text-[9px]"} ${selectedRunId === summary.runId ? "active bg-[#edf3ff] shadow-[inset_2px_0_#2563eb] hover:bg-[#e4edff]" : "bg-transparent hover:bg-[#f4f6f5]"}`}
                  key={summary.runId}
                  onClick={() => onSelectRun(summary.runId)}
                  aria-pressed={selectedRunId === summary.runId}
                  style={rowStyle}
                  aria-label={`${summary.runId}, ${summary.status}, ${runDuration(summary)}, ${
                    triggeredAt
                      ? RUN_TIMESTAMP_FORMAT.format(triggeredAt)
                      : "trigger time not recorded"
                  }`}
                >
                  <span
                    className={`row-span-2 ${summary.status === "requesting" ? "bg-amber" : summary.status === "success" ? "status-success bg-success" : summary.status === "failed" ? "status-failed bg-danger" : summary.status === "running" ? "status-running bg-acid" : "bg-muted"} size-[7px] rounded-full`}
                    aria-hidden="true"
                  />
                  <code title={summary.runId}>{summary.runId}</code>
                  <span
                    className={`run-status-text capitalize ${expanded ? "text-[11px]" : "text-[8px]"} ${summary.status === "requesting" ? "text-amber" : summary.status === "success" ? "status-success text-success" : summary.status === "failed" ? "status-failed text-danger" : summary.status === "running" ? "status-running text-acid" : ""}`}
                  >
                    {summary.status}
                  </span>
                  <time
                    className={`run-duration text-right font-mono text-secondary ${expanded ? "text-[11px]" : "text-[8px]"}`}
                  >
                    {runDuration(summary)}
                  </time>
                  <time
                    className={`run-triggered-at col-start-2 col-end-5 font-mono text-secondary ${expanded ? "text-[11px]" : "text-[8px]"}`}
                    dateTime={triggeredAt?.toISOString()}
                  >
                    {triggeredAt
                      ? RUN_TIMESTAMP_FORMAT.format(triggeredAt)
                      : "Trigger time not recorded"}
                  </time>
                </button>
              );
            })}
          </div>
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
      {expanded && (
        <nav
          aria-label="Run pagination"
          className="flex shrink-0 flex-col gap-3 border-t border-[#eef1ef] px-[20px] py-[16px] text-xs text-secondary"
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
              className="cursor-pointer rounded-md border border-line bg-transparent px-3 py-2 text-ink hover:bg-canvas focus-visible:outline-2 focus-visible:outline-acid disabled:cursor-not-allowed disabled:opacity-40"
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
              className="cursor-pointer rounded-md border border-line bg-transparent px-3 py-2 text-ink hover:bg-canvas focus-visible:outline-2 focus-visible:outline-acid disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </nav>
      )}
    </section>
  );
}
