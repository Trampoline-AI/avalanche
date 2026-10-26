import type { TokenUsage } from "../../generated/agentSchemas";
import type { AgentLiveEvent } from "../common";
import type { AgentTraceStep, TraceTurnTokens } from "../types";
import type { TraceViewer } from "./index";

// Claude prices only the whole session, so a turn's cost is usually absent.
function tokenUsage(usage: TokenUsage | null): TraceTurnTokens | undefined {
  if (usage === null) return undefined;
  return {
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    cost: usage.cost_usd ?? undefined,
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
    outputKind: "response",
    output: text,
    untruncatedOutput: text,
    // Rate-limit, billing and authentication failures arrive as API-error turns.
    error: native.api_error !== null,
    durationMs: turn.duration_ms ?? undefined,
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
    usage: { main: tokenUsage(turn.usage) },
  };
}

function retainedValue(event: AgentLiveEvent, kind: "inputs" | "outputs"): unknown {
  const { native } = event;
  if (kind === "inputs" && native.kind === "started") return native.inputs;
  // A failed session reports no output, and an oversized one is left out of the trace.
  if (kind !== "outputs" || native.kind !== "finished") return undefined;
  const output = native.structured_output;
  if (output === null || typeof output !== "object" || Array.isArray(output)) return undefined;
  // Workspace fields hold the session's working directory, not Claude's JSON.
  const workspaces = Object.fromEntries(
    native.workspace_outputs.map((name) => [
      name,
      "workspace: the session's working directory",
    ]),
  );
  return { ...output, ...workspaces };
}

export const claudeAgentViewer: TraceViewer = {
  turn,
  // The common header already holds the model, usage and cost, so the body is not read.
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
