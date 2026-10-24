import { describe, expect, it } from "vitest";

import {
  finishedTrace,
  runStartedEvent,
  runSucceededEvent,
  tokenUsage,
  turnEvent,
} from "../test/agentTraceFixtures";
import { parseAgentLiveEvent, parseAgentTraceTerminal } from "./common";
import { TRACE_VIEWERS, activityLabel, headerViewer, viewerFor } from "./viewers";

describe("agent trace viewers", () => {
  it("has a viewer for every trace format", () => {
    expect(Object.keys(TRACE_VIEWERS)).toEqual(["predict_rlm.run/v1"]);
  });

  it("maps a PredictRLM turn event from its common turn and native details", () => {
    const event = parseAgentLiveEvent(turnEvent(1, { untruncatedOutput: "full detail" }));

    const step = viewerFor(event.native.format).turn(event);

    expect(step).toMatchObject({
      iteration: 1,
      reasoning: "Reasoning for turn 1.",
      code: 'print("turn 1")',
      output: "Output for turn 1.",
      untruncatedOutput: "full detail",
      error: false,
      lm: { finishReason: "stop" },
      toolCalls: [{ name: "lookup_1", arguments: { args: [], kwargs: {} } }],
      predictCalls: [
        {
          signature: "question -> answer",
          model: "sub-model",
          calls: [{ input: { question: "Ready?" }, output: { answer: "Prediction 1" } }],
        },
      ],
    });
  });

  it("marks a turn whose code failed", () => {
    const event = parseAgentLiveEvent(turnEvent(2, { failed: true }));
    expect(viewerFor(event.native.format).turn(event).error).toBe(true);
  });

  it("rejects a non-turn event as a turn", () => {
    const event = parseAgentLiveEvent(runStartedEvent({ question: "Ready?" }));
    expect(() => viewerFor(event.native.format).turn(event)).toThrow();
  });

  it("reads header details and run values from PredictRLM bodies", () => {
    const viewer = viewerFor("predict_rlm.run/v1");
    expect(
      viewer.summary(
        parseAgentTraceTerminal(
          finishedTrace({ subModel: "sub", maxIterations: 6, sub: tokenUsage(9, 1, 0.5) }),
        ),
      ),
    ).toMatchObject({ subModel: "sub", maxIterations: 6, usage: { sub: { inputTokens: 9 } } });
    const started = parseAgentLiveEvent(runStartedEvent({ question: "Ready?" }));
    const succeeded = parseAgentLiveEvent(runSucceededEvent({ answer: "Yes" }));
    expect(viewer.retainedValue(started, "inputs")).toEqual({ question: "Ready?" });
    expect(viewer.retainedValue(succeeded, "outputs")).toEqual({ answer: "Yes" });
    expect(viewer.retainedValue(started, "outputs")).toBeUndefined();
  });

  it("names live events and refuses unknown formats", () => {
    expect(activityLabel("predict.started")).not.toBe("predict.started");
    expect(activityLabel("custom.kind")).toBe("custom.kind");
    expect(headerViewer("other/v1")).toBeUndefined();
    expect(() => viewerFor("other/v1")).toThrow("No viewer");
  });
});
