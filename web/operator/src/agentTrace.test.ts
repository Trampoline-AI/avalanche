import { describe, expect, it } from "vitest";

import { parseAgentTraceStepEvent } from "./agentTrace";

function tokenUsage(inputTokens = 0) {
  return { input_tokens: inputTokens, output_tokens: 2, cost: 0.01, cache_hits: 0 };
}

function eventDetail() {
  return {
    sequence: 3,
    event_kind: "iteration.recorded",
    timestamp_ns: 42,
    invocation_id: "invocation-1",
    data: {
      iteration: 1,
      step: {
        iteration: 1,
        reasoning: "Inspect the available evidence.",
        code: "print('ready')",
        output: "ready",
        untruncated_output: "ready\nfull detail",
        error: false,
        duration_ms: 1250,
        tool_calls: [
          {
            name: "lookup",
            args: ["record-1"],
            kwargs: { exact: true },
            result: { found: true },
            error: null,
            duration_ms: 40,
          },
        ],
        predict_calls: [
          {
            signature: "question -> answer",
            instructions: "Answer from evidence.",
            model: "sub-model",
            total_usage: tokenUsage(7),
            calls: [
              {
                duration_ms: 300,
                usage: tokenUsage(7),
                input: { question: "Ready?" },
                output: { answer: "Yes" },
                error: null,
                lm: { finish_reason: "stop" },
              },
            ],
          },
        ],
        lm: { finish_reason: "stop" },
        usage: { main: tokenUsage(12), sub: tokenUsage(7) },
      },
    },
  };
}

describe("parseAgentTraceStepEvent", () => {
  it("validates and maps one retained PredictRLM iteration", () => {
    const step = parseAgentTraceStepEvent(eventDetail());

    expect(step).toMatchObject({
      iteration: 1,
      reasoning: "Inspect the available evidence.",
      durationMs: 1250,
      lm: { finishReason: "stop" },
      usage: { main: { inputTokens: 12 }, sub: { inputTokens: 7 } },
      toolCalls: [{ name: "lookup", durationMs: 40 }],
      predictCalls: [
        {
          signature: "question -> answer",
          model: "sub-model",
          calls: [{ input: { question: "Ready?" }, output: { answer: "Yes" } }],
        },
      ],
    });
  });

  it("rejects a retained event without the canonical step shape", () => {
    const valid = eventDetail();
    const detail = {
      ...valid,
      data: {
        ...valid.data,
        step: { ...valid.data.step, tool_calls: [{ name: "lookup" }] },
      },
    };

    expect(() => parseAgentTraceStepEvent(detail)).toThrow(
      "trace step tool_calls[0].args must be an array",
    );
  });

  it("rejects a non-iteration event", () => {
    expect(() => parseAgentTraceStepEvent({ event_kind: "tool.finished", data: {} })).toThrow(
      "agent trace event must be an iteration.recorded event",
    );
  });
});
