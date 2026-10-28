import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { RunStatsBar } from "./RunStatsBar";

it("shows the success rate", async () => {
  vi.useFakeTimers();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      json: async () => ({
        runs: [
          { status: "success", started_at: 0, ended_at: 4 },
          { status: "failed", started_at: 0, ended_at: 2 },
        ],
      }),
    })),
  );
  render(<RunStatsBar workflowSelector="flow" operatorUrl="http://localhost:7435" />);
  await vi.advanceTimersByTimeAsync(5000);
  expect(screen.getByText("Success rate: 50%")).toBeTruthy();
  expect(screen.getByText("Show failed runs")).toBeTruthy();
});
