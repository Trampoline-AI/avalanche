import { useSyncExternalStore } from "react";

/** How often the shared clock re-renders relative times. Minutes are the finest unit shown. */
export const TIME_AGO_TICK_MS = 30_000;

const SHORT_DATE = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const SHORT_DATE_YEAR = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});
const ABSOLUTE = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * A run time relative to `now`: "just now", "3 min ago", "2 h ago", "yesterday",
 * then a short date ("Nov 3", or "Nov 3, 2025" in another year).
 */
export function formatTimeAgo(at: Date | number, now: Date | number = Date.now()): string {
  const then = new Date(at);
  const current = new Date(now);
  const seconds = (current.getTime() - then.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} h ago`;
  if (startOfDay(current) - startOfDay(then) <= 86_400_000 + 3_600_000) return "yesterday";
  return (then.getFullYear() === current.getFullYear() ? SHORT_DATE : SHORT_DATE_YEAR).format(
    then,
  );
}

/** The absolute local timestamp shown on hover beside a relative time. */
export function formatAbsoluteTime(at: Date | number): string {
  return ABSOLUTE.format(new Date(at));
}

// One module-level clock for every relative time on the page: a single interval,
// started by the first subscriber and stopped when the last one leaves.
const listeners = new Set<() => void>();
let now = Date.now();
let timer: ReturnType<typeof setInterval> | undefined;

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (timer === undefined) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      for (const notify of listeners) notify();
    }, TIME_AGO_TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size || timer === undefined) return;
    clearInterval(timer);
    timer = undefined;
  };
}

const snapshot = () => now;

/** Milliseconds since the epoch from the shared clock; re-renders every TIME_AGO_TICK_MS. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export interface TimeAgoProps {
  /** A Date, or epoch milliseconds. */
  at: Date | number;
  className?: string;
}

/** A relative time ("3 min ago") with the absolute local time on hover. */
export function TimeAgo({ at, className }: TimeAgoProps) {
  const current = useNow();
  const date = new Date(at);
  return (
    <time className={className} dateTime={date.toISOString()} title={formatAbsoluteTime(date)}>
      {formatTimeAgo(date, current)}
    </time>
  );
}
