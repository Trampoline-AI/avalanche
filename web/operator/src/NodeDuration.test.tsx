import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NodeDuration } from "./GraphCanvas";

describe("node duration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(performance, "now").mockReturnValue(0);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it.each([
    [0, "0.0s"],
    [119.9, "119.9s"],
    [120, "120.0s"],
    [120.1, "2m 0s"],
    [185.7, "3m 5s"],
    [4140, "69m 0s"],
    [4140.1, "1h 9m 0s"],
    [7384.9, "2h 3m 4s"],
  ])("formats %s seconds and exposes total seconds on hover", (seconds, label) => {
    render(<NodeDuration startedAt={10} runningElapsedSeconds={seconds} running compact />);
    expect(screen.getByText(label).getAttribute("title")).toBe(`${seconds.toFixed(1)} seconds`);
  });

  it.each([
    [120, "2m 1s"],
    [4140, "1h 9m 1s"],
  ])("switches units as a running timer passes %s seconds", (seconds, label) => {
    render(
      <NodeDuration startedAt={10} runningElapsedSeconds={seconds} running compact={false} />,
    );
    vi.mocked(performance.now).mockReturnValue(1000);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText(label).getAttribute("title")).toBe(
      `${(seconds + 1).toFixed(1)} seconds`,
    );
  });

  it("keeps the final elapsed time after the node stops", () => {
    render(
      <NodeDuration
        startedAt={10}
        endedAt={195.7}
        runningElapsedSeconds={0}
        running={false}
        compact
      />,
    );
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("3m 5s").getAttribute("title")).toBe("185.7 seconds");
  });
});
