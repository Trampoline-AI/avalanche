import { describe, expect, it } from "vitest";

import {
  claudeFinishedEvent,
  claudeStartedEvent,
  claudeTurnEvent,
  runStartedEvent,
  runSucceededEvent,
  turnEvent,
} from "../test/agentTraceFixtures";
import { parseAgentLiveEvent } from "./common";
import { TRACE_VIEWERS, activityLabel, viewerFor } from "./viewers";

describe("agent trace viewers", () => {
  it("has a viewer for every trace format", () => {
    expect(Object.keys(TRACE_VIEWERS).sort()).toEqual([
      "claude_agent.messages/v1",
      "predict_rlm.run/v1",
    ]);
  });

  it("maps a PredictRLM turn event from its common turn and its own part", () => {
    const event = parseAgentLiveEvent(turnEvent(1, { untruncatedOutput: "full detail" }));

    const step = viewerFor(event.native.format).turn(event);

    expect(step).toMatchObject({
      iteration: 1,
      message: "Text for turn 1.",
      execution: {
        code: 'print("turn 1")',
        output: "Output for turn 1.",
        untruncatedOutput: "full detail",
      },
      error: false,
      lm: { finishReason: "stop" },
      // The predict tool call is listed with its details, not with the other tools.
      toolCalls: [{ name: "lookup_1", arguments: { args: [], kwargs: {} } }],
      predictCalls: [
        {
          signature: "question -> answer",
          model: "sub-model",
          input: { question: "Ready?" },
          output: { answer: "Prediction 1" },
        },
      ],
      toolCallCount: 1,
      predictCallCount: 1,
    });
  });

  it("marks a turn whose code failed", () => {
    const event = parseAgentLiveEvent(turnEvent(2, { failed: true }));
    expect(viewerFor(event.native.format).turn(event).error).toBe(true);
  });

  it("does not fail a turn whose code handled a failed tool call", () => {
    const body = turnEvent(3);
    if (body.common?.kind !== "turn.finished") throw new Error("fixture is a turn");
    const toolCalls = body.common.turn.tool_calls.map((call) => ({
      ...call,
      error: "not found",
    }));
    const event = parseAgentLiveEvent({
      ...body,
      common: { ...body.common, turn: { ...body.common.turn, tool_calls: toolCalls } },
    });
    expect(viewerFor(event.native.format).turn(event).error).toBe(false);
  });

  it("rejects a non-turn event as a turn", () => {
    const event = parseAgentLiveEvent(runStartedEvent({ question: "Ready?" }));
    expect(() => viewerFor(event.native.format).turn(event)).toThrow();
  });

  it("reads run values from PredictRLM bodies", () => {
    const viewer = viewerFor("predict_rlm.run/v1");
    const started = parseAgentLiveEvent(runStartedEvent({ question: "Ready?" }));
    const succeeded = parseAgentLiveEvent(runSucceededEvent({ answer: "Yes" }));
    expect(viewer.retainedValue(started, "inputs")).toEqual({ question: "Ready?" });
    expect(viewer.retainedValue(succeeded, "outputs")).toEqual({ answer: "Yes" });
    expect(viewer.retainedValue(started, "outputs")).toBeUndefined();
  });

  it("names live events and refuses unknown formats", () => {
    expect(activityLabel("predict.started")).not.toBe("predict.started");
    expect(activityLabel("custom.kind")).toBe("custom.kind");
    expect(() => viewerFor("other/v1")).toThrow("No viewer");
  });

  it("maps a Claude turn from its message, thinking and tool calls", () => {
    const event = parseAgentLiveEvent(claudeTurnEvent(2));
    const viewer = viewerFor(event.native.format);

    expect(viewer.turn(event)).toMatchObject({
      iteration: 2,
      message: "Answer text 2.",
      reasoning: "Thinking 2.",
      error: false,
      toolCalls: [{ name: "mcp__avalanche__lookup", arguments: { name: "a" }, result: "10" }],
      predictCalls: [],
      lm: { finishReason: "tool_use" },
    });
    // Claude runs no generated code, so the turn has no execution.
    expect(viewer.turn(event).execution).toBeUndefined();
    // Claude turns are not timed, so no duration is shown rather than 0ms.
    expect(viewer.turn(event).durationMs).toBeUndefined();
    // No sub-model, so the explorer shows no predict calls or sub-model usage.
    expect(viewer.turn(event).usage.sub).toBeUndefined();
    // Claude prices only the whole session, so a turn has no cost of its own.
    expect(viewer.turn(event).usage.main?.cost).toBeUndefined();
    const body = claudeTurnEvent(3);
    if (body.common?.kind !== "turn.finished") throw new Error("fixture is a turn");
    const { turn } = body.common;
    if (turn.native.format !== "claude_agent.messages/v1") throw new Error("a Claude turn");
    const apiError = parseAgentLiveEvent({
      ...body,
      common: {
        ...body.common,
        turn: { ...turn, native: { ...turn.native, api_error: "rate_limit" } },
      },
    });
    expect(viewer.turn(apiError).error).toBe(true);
    const started = parseAgentLiveEvent(claudeStartedEvent({ a: 2 }));
    const finished = parseAgentLiveEvent(claudeFinishedEvent({ total: 5 }));
    expect(viewer.retainedValue(started, "inputs")).toEqual({ a: 2 });
    expect(viewer.retainedValue(finished, "outputs")).toEqual({ total: 5 });
    const failed = parseAgentLiveEvent(claudeFinishedEvent(null));
    expect(viewer.retainedValue(failed, "outputs")).toBeUndefined();
    const edited = parseAgentLiveEvent(claudeFinishedEvent({ total: 5 }, ["edited"]));
    expect(viewer.retainedValue(edited, "outputs")).toMatchObject({ total: 5 });
    expect(viewer.retainedValue(edited, "outputs")).toHaveProperty("edited");
  });
});
