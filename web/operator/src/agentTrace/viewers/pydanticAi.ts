import type { TokenUsage } from "../../generated/agentSchemas";
import { finishedTurn, type AgentLiveEvent } from "../common";
import type { TraceHeaderMsg } from "../../model";
import type { AgentTraceStep, TraceHeaderLine, TraceTurnTokens } from "../types";
import { headerUsageText, toolCall } from "./common";
import type { TraceViewer } from "./index";

// Pydantic AI hands back the step's outputs by calling this tool.
const ANSWER_TOOL = "final_result";

// Input counts the prompt cache too, as the run header does.
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
  if (event.native !== null) throw new Error("Pydantic AI turn events have no native part");
  const { turn } = common;
  const response = turn.native;
  if (response.format !== "pydantic_ai.messages/v1") {
    throw new Error(`not a Pydantic AI turn: ${response.format}`);
  }
  return {
    iteration: turn.index,
    message: turn.message ?? undefined,
    reasoning: turn.reasoning ?? undefined,
    error: response.finish_reason === "error",
    durationMs: turn.duration_ms ?? undefined,
    toolCalls: turn.tool_calls.map(toolCall),
    predictCalls: [],
    toolCallCount: turn.tool_calls.length + common.omitted_tool_calls,
    predictCallCount: 0,
    lm: { finishReason: response.finish_reason ?? undefined },
    usage: { main: tokenUsage(turn.usage) },
    submitsAnswer:
      common.omitted_tool_calls === 0 &&
      turn.tool_calls.length > 0 &&
      turn.tool_calls.every((call) => call.name === ANSWER_TOOL && call.error === null),
  };
}

function headerLines(header: TraceHeaderMsg): TraceHeaderLine[] {
  const model = header.model ?? "—";
  const lines: TraceHeaderLine[] = [
    { text: header.usage ? `${model} · ${headerUsageText(header.usage)}` : model },
    { text: `${header.turnCount} turns`, muted: true },
  ];
  const { native } = header;
  if (native.format === "pydantic_ai.messages/v1" && native.retries > 0) {
    lines.push({ text: `${native.retries} retried calls`, muted: true });
  }
  return lines;
}

export const pydanticAiViewer: TraceViewer = {
  turn,
  headerLines,
  turnUnit: "turns",
  activityLabels: {
    "run.initialized": "Agent run initialized",
    "tool.retried": "Tool call retried",
  },
};
