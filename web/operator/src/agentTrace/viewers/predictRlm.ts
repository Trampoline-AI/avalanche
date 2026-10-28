import type { PredictRLMTokenUsage } from "../../generated/agentSchemas";
import { finishedTurn, type AgentLiveEvent } from "../common";
import type { TraceHeaderMsg } from "../../model";
import type { AgentTraceStep, TraceHeaderLine, TraceTokenUsage } from "../types";
import { toolCall, usageText } from "./common";
import type { TraceViewer } from "./index";

function tokenUsage(usage: PredictRLMTokenUsage): TraceTokenUsage {
  return {
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    cost: usage.cost,
    cacheHits: usage.cache_hits,
  };
}

function turn(event: AgentLiveEvent): AgentTraceStep {
  const common = finishedTurn(event);
  const { native } = event;
  // A turn its live event kept whole has no native part; one that left calls out says so.
  if (native !== null && native.kind !== "iteration.recorded") {
    throw new Error("PredictRLM turn events carry only iteration.recorded details");
  }
  const { turn } = common;
  const part = turn.native;
  if (part.format !== "predict_rlm.run/v1") {
    throw new Error(`not a PredictRLM turn: ${part.format}`);
  }
  // The turn's last tool calls are its predict calls, in the order of their details.
  const toolCount = turn.tool_calls.length - part.predict_calls.length;
  const predictCalls = turn.tool_calls.slice(toolCount);
  const omissions = native?.omissions;
  return {
    iteration: turn.index,
    message: turn.message ?? undefined,
    reasoning: turn.reasoning ?? undefined,
    execution: {
      code: part.code,
      output: part.output,
      untruncatedOutput: part.untruncated_output,
    },
    // Only failed code fails a turn; the code may have handled a failed tool call.
    error: part.error,
    durationMs: turn.duration_ms ?? undefined,
    toolCalls: turn.tool_calls.slice(0, toolCount).map(toolCall),
    predictCalls: part.predict_calls.map((detail, index) => {
      const call = predictCalls[index];
      if (call === undefined) throw new Error("a predict call has no tool call");
      return {
        signature: detail.signature,
        instructions: detail.instructions ?? undefined,
        model: detail.model,
        durationMs: call.duration_ms ?? undefined,
        usage: tokenUsage(detail.usage),
        input: call.arguments,
        output: call.result,
        error: call.error ?? undefined,
        lm: { finishReason: detail.finish_reason ?? undefined },
      };
    }),
    toolCallCount: toolCount + (omissions?.tool_count ?? 0),
    predictCallCount: part.predict_calls.length + (omissions?.predict_count ?? 0),
    lm: { finishReason: part.finish_reason ?? undefined },
    usage: { main: tokenUsage(part.usage.main), sub: tokenUsage(part.usage.sub) },
    // PredictRLM submits its answer from the code it runs, not with a tool call.
    submitsAnswer: false,
  };
}

// The main model and its sub-model are priced separately; iterations have a limit.
function headerLines(header: TraceHeaderMsg): TraceHeaderLine[] {
  const native = header.native;
  if (native.format !== "predict_rlm.run/v1") {
    throw new Error(`not a PredictRLM trace header: ${native.format}`);
  }
  const { main, sub } = native.usage;
  return [
    {
      label: "Main",
      text: `${header.model ?? "—"} · ${usageText(main.input_tokens, main.output_tokens, main.cost)}`,
    },
    {
      label: "Sub",
      text: `${native.sub_model ?? "—"} · ${usageText(sub.input_tokens, sub.output_tokens, sub.cost)}`,
    },
    { text: `${header.turnCount} of ${native.max_iterations} iterations`, muted: true },
  ];
}

export const predictRlmViewer: TraceViewer = {
  turn,
  headerLines,
  turnUnit: "iterations",
  activityLabels: {
    "iteration.recorded": "Turn finished",
    "code.generated": "Generating code",
    "code.executed": "Running generated code",
    "predict.started": "Calling sub-model",
    "predict.finished": "Sub-model call finished",
    "run.failed": "Agent run failed",
    "run.cancelled": "Agent run cancelled",
  },
};
