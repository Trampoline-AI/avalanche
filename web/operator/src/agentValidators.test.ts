import { describe, expect, it } from "vitest";

import type { AgentTraceRecord } from "./generated/agentSchemas";
import { validateAgentEvent, validateAgentTraceRecord } from "./generated/agentValidators";

const record: AgentTraceRecord = {
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
    cache_hits: 0,
  },
};

describe("generated agent validators", () => {
  it("accept a trace record shaped like the Python model", () => {
    expect(validateAgentTraceRecord(record)).toBe(true);
  });

  it.each([
    ["an unknown runtime", { ...record, header: { ...record.header, runtime_kind: "other" } }],
    ["an unknown field", { ...record, extra: true }],
    ["an old schema version", { ...record, schema_version: 1 }],
    [
      "an unknown action kind",
      { ...record, turns: [{ ...record.turns[0], actions: [{ kind: "other" }] }] },
    ],
  ])("reject a trace record with %s", (_case, body) => {
    expect(validateAgentTraceRecord(body)).toBe(false);
  });

  it("tell live events apart by kind", () => {
    expect(validateAgentEvent({ kind: "tool.started", call_id: "t", name: "lookup" })).toBe(
      true,
    );
    expect(validateAgentEvent({ kind: "tool.started", call_id: "t" })).toBe(false);
    expect(validateAgentEvent({ kind: "unknown", call_id: "t", name: "lookup" })).toBe(false);
  });
});
