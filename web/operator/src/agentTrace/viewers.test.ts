import { describe, expect, it } from "vitest";

import {
  claudeFinishedEvent,
  claudeStartedEvent,
  claudeTurnEvent,
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
    expect(Object.keys(TRACE_VIEWERS).sort()).toEqual([
      "claude_agent.messages/v1",
      "predict_rlm.run/v1",
    ]);
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

  it("does not fail a turn whose code handled a failed tool call", () => {
    const body = turnEvent(3);
    if (body.common?.kind !== "turn.finished") throw new Error("fixture is a turn");
    const actions = body.common.turn.actions.map((action) =>
      action.kind === "tool_call" ? { ...action, error: "not found" } : action,
    );
    const event = parseAgentLiveEvent({
      ...body,
      common: { ...body.common, turn: { ...body.common.turn, actions } },
    });
    expect(viewerFor(event.native.format).turn(event).error).toBe(false);
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

  it("maps a Claude turn from its text, thinking and tool calls", () => {
    const event = parseAgentLiveEvent(claudeTurnEvent(2));
    const viewer = viewerFor(event.native.format);

    expect(viewer.turn(event)).toMatchObject({
      iteration: 2,
      reasoning: "Thinking 2.",
      output: "Answer text 2.",
      error: false,
      toolCalls: [{ name: "mcp__avalanche__lookup", arguments: { name: "a" }, result: "10" }],
      predictCalls: [],
      lm: { finishReason: "tool_use" },
    });
    expect(viewer.turn(event).outputKind).toBe("response");
    expect(viewer.turn(event)).not.toHaveProperty("code");
    // Claude turns are not timed, so no duration is shown rather than 0ms.
    expect(viewer.turn(event).durationMs).toBeUndefined();
    expect(viewer.hasSubModel).toBe(false);
    const body = claudeTurnEvent(3);
    if (body.native.kind !== "message.finished") throw new Error("fixture is a turn");
    const apiError = parseAgentLiveEvent({
      ...body,
      native: { ...body.native, api_error: "rate_limit" },
    });
    expect(viewer.turn(apiError).error).toBe(true);
    const started = parseAgentLiveEvent(claudeStartedEvent({ a: 2 }));
    const finished = parseAgentLiveEvent(claudeFinishedEvent({ total: 5 }));
    expect(viewer.retainedValue(started, "inputs")).toEqual({ a: 2 });
    expect(viewer.retainedValue(finished, "outputs")).toEqual({ total: 5 });
    const failed = parseAgentLiveEvent(claudeFinishedEvent(null));
    expect(viewer.retainedValue(failed, "outputs")).toBeUndefined();
  });
});
