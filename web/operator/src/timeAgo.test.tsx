import { act, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { TIME_AGO_TICK_MS, TimeAgo, formatAbsoluteTime, formatTimeAgo } from "./timeAgo";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const now = new Date(2026, 10, 10, 20, 0, 0);

it.each([
  [new Date(2026, 10, 10, 19, 59, 30), "just now"],
  [new Date(2026, 10, 10, 20, 0, 5), "just now"],
  [new Date(2026, 10, 10, 19, 57, 0), "3 min ago"],
  [new Date(2026, 10, 10, 17, 30, 0), "2 h ago"],
  [new Date(2026, 10, 9, 8, 0, 0), "yesterday"],
])("formats %s as %s", (at, label) => {
  expect(formatTimeAgo(at, now)).toBe(label);
});

it("falls back to a short date, with the year only in another year", () => {
  const shortDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  expect(formatTimeAgo(new Date(2026, 10, 3, 9), now)).toBe(
    shortDate.format(new Date(2026, 10, 3)),
  );
  expect(formatTimeAgo(new Date(2025, 10, 3, 9), now)).toContain("2025");
});

it("shows the absolute local time on hover", () => {
  vi.useFakeTimers({ now });
  const at = new Date(2026, 10, 10, 19, 57);
  const view = render(<TimeAgo at={at} />);
  const time = view.container.querySelector("time")!;
  expect(time).toHaveTextContent("3 min ago");
  expect(time).toHaveAttribute("title", formatAbsoluteTime(at));
  expect(time).toHaveAttribute("dateTime", at.toISOString());
});

it("drives every mounted TimeAgo from one interval, cleared when the last unmounts", () => {
  vi.useFakeTimers({ now });
  const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
  const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
  const at = new Date(2026, 10, 10, 19, 59, 30);
  const view = render(
    <>
      {Array.from({ length: 25 }, (_, index) => (
        <TimeAgo key={index} at={at} />
      ))}
    </>,
  );
  const clockIntervals = setIntervalSpy.mock.calls.filter(([, ms]) => ms === TIME_AGO_TICK_MS);
  expect(clockIntervals).toHaveLength(1);
  expect(vi.getTimerCount()).toBe(1);
  expect(view.container.querySelectorAll("time")[24]).toHaveTextContent("just now");

  act(() => vi.advanceTimersByTime(TIME_AGO_TICK_MS * 4));
  for (const time of view.container.querySelectorAll("time"))
    expect(time).toHaveTextContent("2 min ago");

  view.unmount();
  expect(clearIntervalSpy).toHaveBeenCalledTimes(1);
  expect(vi.getTimerCount()).toBe(0);
});
