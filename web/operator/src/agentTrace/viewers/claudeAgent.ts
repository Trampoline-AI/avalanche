import type { TokenUsage } from "../../generated/agentSchemas";
import type { AgentLiveEvent } from "../common";
import type { AgentTraceStep, TraceTokenUsage } from "../types";
import type { TraceViewer } from "./index";

const NO_USAGE: TraceTokenUsage = { inputTokens: 0, outputTokens: 0, cost: 0, cacheHits: 0 };

function tokenUsage(usage: TokenUsage | null): TraceTokenUsage {
  if (usage === null) return NO_USAGE;
  return {
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    cost: usage.cost_usd ?? 0,
    cacheHits: 0,
  };
}

function turn(event: AgentLiveEvent): AgentTraceStep {
  const { common, native } = event;
  if (common?.kind !== "turn.finished" || native.kind !== "message.finished") {
    throw new Error("Claude turn events are message.finished with a finished turn");
  }
  const { turn } = common;
  const text = turn.text ?? "";
  return {
    iteration: turn.index,
    reasoning: turn.reasoning ?? "",
    // Claude answers in text and tool calls; it runs no generated code of its own.
    code: "",
    output: text,
    untruncatedOutput: text,
    error: false,
    durationMs: turn.duration_ms ?? 0,
    toolCalls: turn.actions.flatMap((action) =>
      action.kind === "tool_call"
        ? [
            {
              name: action.name,
              arguments: action.arguments,
              result: action.result,
              error: action.error ?? undefined,
              durationMs: action.duration_ms ?? undefined,
            },
          ]
        : [],
    ),
    predictCalls: [],
    lm: { finishReason: native.stop_reason ?? undefined },
    usage: { main: tokenUsage(turn.usage), sub: NO_USAGE },
  };
}

function retainedValue(event: AgentLiveEvent, kind: "inputs" | "outputs"): unknown {
  const { native } = event;
  if (kind === "inputs" && native.kind === "started") return native.inputs;
  if (kind === "outputs" && native.kind === "finished") return native.structured_output;
  return undefined;
}

export const claudeAgentViewer: TraceViewer = {
  turn,
  // The common header already holds the model, usage and cost.
  summary: () => undefined,
  usageInBody: false,
  hasSubModel: false,
  turnUnit: "turns",
  activityLabels: {
    started: "Prompt sent",
    "session.initialized": "Session started",
    "tool.used": "Calling tool",
    "tool.returned": "Tool call finished",
    "message.finished": "Turn finished",
    rate_limit: "Rate limit reported",
    finished: "Session finished",
  },
  valueEventKinds: { inputs: "started", outputs: "finished" },
  retainedValue,
};
