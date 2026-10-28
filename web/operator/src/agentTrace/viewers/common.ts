import type { ToolCall } from "../../generated/agentSchemas";
import type { TokenUsageMsg } from "../../model";
import type { AgentTraceToolCall } from "../types";

const TOKEN_NUMBER_FORMAT = new Intl.NumberFormat();

/** "1,200 in / 80 out", then the cost when it is known. */
export function usageText(inputTokens: number, outputTokens: number, costUsd?: number): string {
  const tokens = `${TOKEN_NUMBER_FORMAT.format(inputTokens)} in / ${TOKEN_NUMBER_FORMAT.format(outputTokens)} out`;
  return costUsd === undefined ? tokens : `${tokens} · $${costUsd.toFixed(4)}`;
}

/** A header's shared usage; input counts prompt-cache reads and writes too. */
export function headerUsageText(usage: TokenUsageMsg): string {
  const input =
    Number(usage.inputTokens) +
    Number(usage.cacheReadInputTokens ?? 0) +
    Number(usage.cacheCreationInputTokens ?? 0);
  return usageText(input, Number(usage.outputTokens), usage.costUsd);
}

export function toolCall(call: ToolCall): AgentTraceToolCall {
  return {
    name: call.name,
    arguments: call.arguments,
    result: call.result,
    error: call.error ?? undefined,
    durationMs: call.duration_ms ?? undefined,
  };
}
