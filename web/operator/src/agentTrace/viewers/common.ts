import type { ToolCall } from "../../generated/agentSchemas";
import type { AgentTraceToolCall } from "../types";

export function toolCall(call: ToolCall): AgentTraceToolCall {
  return {
    name: call.name,
    arguments: call.arguments,
    result: call.result,
    error: call.error ?? undefined,
    durationMs: call.duration_ms ?? undefined,
  };
}
