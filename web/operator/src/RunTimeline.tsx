import { type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { formatElapsedSeconds } from "./elapsedDuration";
import { formatRemaining } from "./machineGraph";
import type { PendingItem, TimelineEntry, TimelineTone } from "./timelineEntries";

/** What a hovered or selected row points at on the graph. */
export type TimelineFocus = Pick<TimelineEntry, "id" | "state" | "arc" | "nodeId" | "entry">;

/** Rows rendered at once; earlier rows load in pages of this size. */
export const TIMELINE_PAGE = 200;

const TONE_CLASS: Record<TimelineTone, string> = {
  neutral: "bg-secondary",
  ok: "bg-success",
  warn: "bg-amber",
  danger: "bg-danger",
  muted: "bg-line-strong",
};

/** The time column's one meaning, said once in its header. */
export const TIME_COLUMN_LABEL = "+ since start";

/** A duration on the row: "0.2s", or "<0.1s" for a step too quick to read. */
export function durationLabel(seconds: number) {
  return seconds < 0.05 ? "<0.1s" : formatElapsedSeconds(seconds);
}

function offsetLabel(offset: number | undefined) {
  if (offset === undefined) return "—";
  return `+${formatElapsedSeconds(Math.max(0, offset))}`;
}

/** The time cell's tooltip: the local time and the offset, or why there is none. */
export function timeTooltip(entry: Pick<TimelineEntry, "offset" | "at" | "timeNote">) {
  if (entry.offset === undefined)
    return `Time unknown: ${entry.timeNote ?? "the host has not reported when this happened."}`;
  const since = `${offsetLabel(entry.offset)} since the run started`;
  return entry.at !== undefined
    ? `${new Date(entry.at * 1000).toLocaleString()} (${since})`
    : since;
}

const ROW_GRID = "grid grid-cols-[72px_10px_minmax(0,1fr)_auto] items-start gap-2 px-[9px]";

function Countdown({ due }: { due: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);
  const remaining = Math.max(0, Math.ceil(due - now));
  return (
    <span className="text-amber" data-timer-remaining={remaining}>
      {remaining > 0 ? `${formatRemaining(remaining)} left` : "due, awaiting the host"}
    </span>
  );
}

export interface RunTimelineProps {
  /** Entries to list, oldest first (already narrowed by `filter`). */
  entries: readonly TimelineEntry[];
  /** What the run waits for now, pinned above the list. */
  pending: readonly PendingItem[];
  /**
   * The graph selection narrowing the list (its label and clear show in the
   * pane's scope chip).
   */
  filter?: { label: string };
  selectedId?: string;
  onHover: (focus: TimelineFocus | undefined) => void;
  onSelect: (entry: TimelineEntry | undefined) => void;
  /** Opens a machine state entry's steps on the graph (absent: no Steps action). */
  onOpenStateEntry?: (entry: number) => void;
  /** Shows the entry's step or state entry logs in the Logs tab (absent: no Logs action). */
  onOpenLogs?: (entry: TimelineEntry) => void;
  /** An inline action for a pending row (a decision form). */
  renderPending?: (item: PendingItem) => ReactNode;
  /** Above the list: the open state entry's breadcrumb. */
  header?: ReactNode;
  /** Below the list: paging for earlier state entries. */
  footer?: ReactNode;
  /** One neutral line about entries this host does not report. */
  note?: string;
  loading?: boolean;
  error?: ReactNode;
}

/**
 * The run's events in time order, the same for flow and machine runs. Hovering a
 * row highlights its state, arc or step on the graph; selecting one keeps it
 * highlighted and offers its steps and logs.
 */
export function RunTimeline({
  entries,
  pending,
  filter,
  selectedId,
  onHover,
  onSelect,
  onOpenStateEntry,
  onOpenLogs,
  renderPending,
  header,
  footer,
  note,
  loading = false,
  error,
}: RunTimelineProps) {
  const [shown, setShown] = useState(TIMELINE_PAGE);
  const visible = useMemo(
    () => (entries.length > shown ? entries.slice(entries.length - shown) : entries),
    [entries, shown],
  );
  const hidden = entries.length - visible.length;
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  // Newest at the bottom, followed like the logs unless the reader scrolled up.
  useLayoutEffect(() => {
    const element = scroll.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [visible.length, filter?.label]);
  const [openPending, setOpenPending] = useState<string>();
  return (
    <section
      aria-label="Run timeline"
      className="run-timeline grid min-h-0 min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] font-mono text-[10px]"
      onMouseLeave={() => onHover(undefined)}
    >
      <div>{header}</div>
      {pending.length > 0 ? (
        <ul
          aria-label="Pending"
          className="run-timeline-pending m-0 list-none border-b border-line bg-panel p-0"
        >
          {pending.map((item) => (
            <li
              key={item.id}
              data-pending-kind={item.kind}
              data-wait-kind={item.kind === "deferred" ? "event" : item.kind}
              data-state={item.state}
              className={`${ROW_GRID} border-b border-log-line py-1.5 last:border-0`}
              onMouseEnter={() => onHover({ ...item, id: `pending:${item.id}` })}
            >
              <span className="text-[8px] tracking-[.12em] text-amber uppercase">Pending</span>
              <span
                aria-hidden="true"
                className="mt-1 size-2 rounded-full border border-amber"
              />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                <span className="text-ink">{item.title}</span>
                {item.detail && <span className="text-secondary"> · {item.detail}</span>}
                {item.due !== undefined && (
                  <span className="text-secondary">
                    {" "}
                    · <Countdown due={item.due} />
                  </span>
                )}
                {openPending === item.id && renderPending?.(item)}
              </span>
              {renderPending && item.card?.kind === "decision" && (
                <button
                  type="button"
                  className="cursor-pointer rounded border border-line bg-panel px-1.5 py-0.5 text-[9px] text-acid"
                  aria-expanded={openPending === item.id}
                  onClick={() => setOpenPending(openPending === item.id ? undefined : item.id)}
                >
                  {openPending === item.id ? "Close" : "Decide"}
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <div />
      )}
      <div
        ref={scroll}
        className="run-timeline-scroll min-h-0 overflow-auto"
        onScroll={(event) => {
          const element = event.currentTarget;
          following.current =
            element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        }}
      >
        {hidden > 0 && (
          <button
            type="button"
            className="m-2 cursor-pointer rounded border border-line bg-panel px-2 py-[5px] text-[8px] text-acid"
            onClick={() => {
              following.current = false;
              setShown((count) => count + TIMELINE_PAGE);
            }}
          >
            Show {Math.min(hidden, TIMELINE_PAGE)} earlier entries
          </button>
        )}
        {error}
        {visible.length === 0 ? (
          <p className="m-3 text-[11px] text-muted" role={loading ? "status" : undefined}>
            {loading
              ? "Loading the run's timeline…"
              : filter
                ? "No entries for this selection."
                : "No events recorded for this run yet."}
          </p>
        ) : (
          <>
            <div
              className={`run-timeline-columns sticky top-0 z-[1] ${ROW_GRID} border-b border-line bg-panel py-1 text-[8px] tracking-[.12em] text-muted uppercase`}
              data-timeline-columns
            >
              <span
                className="text-right whitespace-nowrap normal-case tracking-normal"
                title="Each row's time is how long after the run started it happened. Hover a time for the local clock time."
                data-time-column-label
              >
                {TIME_COLUMN_LABEL}
              </span>
              <span />
              <span>What happened</span>
              <span />
            </div>
            <ol aria-label="Run events" className="m-0 list-none p-0">
              {visible.map((entry) => {
                const selected = entry.id === selectedId;
                const actions =
                  selected &&
                  ((onOpenStateEntry && entry.entry !== undefined) ||
                    (onOpenLogs && (entry.nodeId !== undefined || entry.entry !== undefined)));
                return (
                  <li
                    key={entry.id}
                    data-entry-id={entry.id}
                    data-timeline-kind={entry.kind}
                    data-status={entry.status}
                    data-state={entry.state}
                    data-entry={entry.entry}
                    data-node-id={entry.nodeId}
                    className={`run-timeline-row ${ROW_GRID} border-b border-log-line py-1.5 ${selected ? "bg-selected shadow-[inset_2px_0_var(--color-acid)]" : "hover:bg-hover"}`}
                    onMouseEnter={() => onHover(entry)}
                  >
                    <time
                      className="text-right whitespace-nowrap text-muted tabular-nums"
                      data-time-unknown={entry.offset === undefined || undefined}
                      dateTime={
                        entry.at !== undefined
                          ? new Date(entry.at * 1000).toISOString()
                          : undefined
                      }
                      title={timeTooltip(entry)}
                    >
                      {offsetLabel(entry.offset)}
                    </time>
                    <span
                      aria-hidden="true"
                      className={`mt-1 size-2 rounded-full ${TONE_CLASS[entry.tone]}`}
                    />
                    <button
                      type="button"
                      aria-pressed={selected}
                      className="min-w-0 cursor-pointer border-0 bg-transparent p-0 text-left [font:inherit] [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
                      onFocus={() => onHover(entry)}
                      onBlur={() => onHover(undefined)}
                      onClick={() => onSelect(selected ? undefined : entry)}
                    >
                      <span className="text-ink">{entry.title}</span>
                      {entry.detail && (
                        <span className="text-secondary"> · {entry.detail}</span>
                      )}
                      {entry.took !== undefined && (
                        <span className="whitespace-nowrap text-secondary" data-took>
                          {" "}
                          · took {durationLabel(entry.took)}
                        </span>
                      )}
                    </button>
                    <span className="flex gap-1">
                      {actions && onOpenStateEntry && entry.entry !== undefined && (
                        <button
                          type="button"
                          className="cursor-pointer rounded border border-line bg-panel px-1.5 py-0.5 text-[9px] text-acid"
                          onClick={() => onOpenStateEntry(entry.entry!)}
                        >
                          Steps
                        </button>
                      )}
                      {actions && onOpenLogs && (
                        <button
                          type="button"
                          className="cursor-pointer rounded border border-line bg-panel px-1.5 py-0.5 text-[9px] text-acid"
                          onClick={() => onOpenLogs(entry)}
                        >
                          Logs
                        </button>
                      )}
                    </span>
                  </li>
                );
              })}
            </ol>
          </>
        )}
        {footer}
        {note && (
          <p className="run-timeline-note m-0 px-[9px] py-2 text-[9px] text-muted" data-note>
            {note}
          </p>
        )}
      </div>
    </section>
  );
}
