import type { TokenUsage } from "../../generated/agentSchemas";
import { finishedTurn, type AgentLiveEvent } from "../common";
import type { TraceHeaderMsg } from "../../model";
import type { AgentTraceStep, TraceHeaderLine, TraceTurnTokens } from "../types";
import { headerUsageText, toolCall } from "./common";
import type { TraceViewer } from "./index";

// OpenAI prices nothing in the trace. Avalanche counts cached input apart from
// input_tokens, so a turn's whole input adds them back.
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
  if (event.native !== null)
    throw new Error("OpenAI Agents SDK turn events have no native part");
  const { turn } = common;
  const response = turn.native;
  if (response.format !== "openai_agents.responses/v1") {
    throw new Error(`not an OpenAI Agents SDK turn: ${response.format}`);
  }
  return {
    iteration: turn.index,
    message: turn.message ?? undefined,
    reasoning: turn.reasoning ?? undefined,
    // A refusal is the model declining the task.
    error: response.refusal !== null,
    durationMs: turn.duration_ms ?? undefined,
    toolCalls: turn.tool_calls.map(toolCall),
    predictCalls: [],
    toolCallCount: turn.tool_calls.length + common.omitted_tool_calls,
    predictCallCount: 0,
    lm: { finishReason: response.answer ? "answer" : undefined },
    usage: { main: tokenUsage(turn.usage) },
    // The answer is the response's own JSON text, not a tool call.
    submitsAnswer: false,
  };
}

// One model; the turn limit is the run's max_turns.
function headerLines(header: TraceHeaderMsg): TraceHeaderLine[] {
  const native = header.native;
  if (native.format !== "openai_agents.responses/v1") {
    throw new Error(`not an OpenAI Agents SDK trace header: ${native.format}`);
  }
  const model = header.model ?? "—";
  return [
    { text: header.usage ? `${model} · ${headerUsageText(header.usage)}` : model },
    { text: `${header.turnCount} of ${native.max_turns} turns`, muted: true },
  ];
}

export const openaiAgentViewer: TraceViewer = {
  turn,
  headerLines,
  turnUnit: "turns",
  activityLabels: {
    "agent.started": "Agent started",
  },
};
