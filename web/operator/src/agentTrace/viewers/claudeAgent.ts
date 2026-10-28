import type { TokenUsage } from "../../generated/agentSchemas";
import { finishedTurn, type AgentLiveEvent } from "../common";
import type { AgentTraceStep, TraceTurnTokens } from "../types";
import { toolCall } from "./common";
import type { TraceViewer } from "./index";

// Claude prices only the whole session, so a turn's cost is usually absent. Claude's
// input_tokens leaves out the prompt cache, which holds most of a turn's input.
function tokenUsage(usage: TokenUsage | null): TraceTurnTokens | undefined {
  if (usage === null) return undefined;
  return {
    inputTokens:
      usage.input_tokens +
      (usage.cache_read_input_tokens ?? 0) +
      (usage.cache_creation_input_tokens ?? 0),
    outputTokens: usage.output_tokens,
    cost: usage.cost_usd ?? undefined,
    cacheHits: 0,
  };
}

function turn(event: AgentLiveEvent): AgentTraceStep {
  const common = finishedTurn(event);
  if (event.native !== null) throw new Error("Claude turn events have no native part");
  const { turn } = common;
  const response = turn.native;
  if (response.format !== "claude_agent.messages/v1") {
    throw new Error(`not a Claude turn: ${response.format}`);
  }
  return {
    iteration: turn.index,
    message: turn.message ?? undefined,
    reasoning: turn.reasoning ?? undefined,
    // Rate-limit, billing and authentication failures arrive as API-error turns.
    error: response.api_error !== null,
    durationMs: turn.duration_ms ?? undefined,
    toolCalls: turn.tool_calls.map(toolCall),
    predictCalls: [],
    toolCallCount: turn.tool_calls.length + common.omitted_tool_calls,
    predictCallCount: 0,
    lm: { finishReason: response.stop_reason ?? undefined },
    usage: { main: tokenUsage(turn.usage) },
    // Claude hands back the step's outputs by calling StructuredOutput.
    submitsAnswer:
      common.omitted_tool_calls === 0 &&
      turn.tool_calls.length > 0 &&
      turn.tool_calls.every((call) => call.name === "StructuredOutput"),
  };
}

export const claudeAgentViewer: TraceViewer = {
  turn,
  turnUnit: "turns",
  activityLabels: {
    "session.initialized": "Session started",
    rate_limit: "Rate limit reported",
  },
};
