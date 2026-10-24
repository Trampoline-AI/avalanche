import { describe, expect, it } from "vitest";

import { parseAgentDeclaration } from "./GraphCanvas";
import { AgentLiveEvent, AgentTrace, AgentTraceTerminal } from "./generated/agentSchemas";
import { finishedTrace, tokenUsage, turnEvent } from "./test/agentTraceFixtures";
import { agentDeclarationJson } from "./test/fixtures";

const record: AgentTrace = {
  schema_version: 2,
  turns_streamed: false,
  header: {
    runtime_kind: "predict_rlm",
    trace_format: "predict_rlm.run/v1",
    status: "succeeded",
    stop_reason: "submit",
    model: "openai/gpt-5.5",
    duration_ms: 20,
    usage: {
      input_tokens: 10,
      output_tokens: 5,
      cache_read_input_tokens: null,
      cache_creation_input_tokens: null,
      cost_usd: null,
    },
    turn_count: 1,
    tool_call_count: 1,
    external_run_id: "run-1",
    session_id: null,
  },
  turns: [
    {
      index: 1,
      text: null,
      reasoning: "look it up",
      duration_ms: 7,
      usage: null,
      actions: [
        {
          kind: "tool_call",
          call_id: null,
          caller: "code",
          name: "lookup",
          arguments: { args: ["q"], kwargs: {} },
          result: "a",
          error: null,
          duration_ms: 2,
        },
      ],
    },
  ],
  native: {
    format: "predict_rlm.run/v1",
    sub_model: null,
    max_iterations: 4,
    usage: { main: tokenUsage(), sub: tokenUsage() },
    telemetry_ref: null,
  },
};

describe("generated agent schemas", () => {
  it("accept a trace record shaped like the Python model", () => {
    expect(AgentTrace.parse(record)).toEqual(record);
  });

  it.each([
    ["an unknown runtime", { ...record, header: { ...record.header, runtime_kind: "other" } }],
    ["an unknown field", { ...record, extra: true }],
    ["an old schema version", { ...record, schema_version: 1 }],
    ["a negative duration", { ...record, header: { ...record.header, duration_ms: -1 } }],
    [
      "an unknown action kind",
      { ...record, turns: [{ ...record.turns[0], actions: [{ kind: "other" }] }] },
    ],
  ])("reject a trace record with %s", (_case, body) => {
    expect(AgentTrace.safeParse(body).success).toBe(false);
  });

  it("accept live event and final trace bodies shaped like the Python models", () => {
    expect(AgentLiveEvent.parse(turnEvent(1))).toEqual(turnEvent(1));
    expect(AgentTraceTerminal.parse(finishedTrace())).toEqual(finishedTrace());
  });

  it.each([
    ["a v1 body", { event_kind: "iteration.recorded", data: {} }],
    ["an old schema version", { ...turnEvent(1), schema_version: 1 }],
    ["an unknown native format", { ...turnEvent(1), native: { format: "other/v1" } }],
    [
      "an unknown native kind",
      { ...turnEvent(1), native: { format: "predict_rlm.run/v1", kind: "code.compiled" } },
    ],
    ["an unknown common kind", { ...turnEvent(1), common: { kind: "turn.paused" } }],
  ])("reject a live event with %s", (_case, body) => {
    expect(AgentLiveEvent.safeParse(body).success).toBe(false);
  });
});

describe("parseAgentDeclaration", () => {
  it("reads a published declaration", () => {
    const declaration = parseAgentDeclaration(
      agentDeclarationJson({
        instructions: "Answer.",
        inputs: [{ name: "question", annotation: "str" }],
        runtime: { settings: { max_iterations: 3 } },
      }),
    );
    expect(declaration?.inputs).toEqual([{ name: "question", type: "str", description: "" }]);
    expect(declaration?.runtime).toEqual({ max_iterations: 3 });
  });

  it("throws on a malformed declaration instead of dropping fields", () => {
    const declaration = JSON.parse(agentDeclarationJson({ instructions: "Answer." }));
    delete declaration.runtime.kind;
    expect(() => parseAgentDeclaration(JSON.stringify(declaration))).toThrow();
  });
});
