import type { PredictRLMTokenUsage } from "../../generated/agentSchemas";
import type { AgentLiveEvent, AgentTraceTerminal } from "../common";
import type { AgentTraceStep, TraceBodySummary, TraceTokenUsage } from "../types";
import { toolCall } from "./common";
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
  const { common, native } = event;
  if (common?.kind !== "turn.finished" || native.kind !== "iteration.recorded") {
    throw new Error("PredictRLM turn events are iteration.recorded with a finished turn");
  }
  const { turn } = common;
  const part = turn.native;
  if (part.format !== "predict_rlm.run/v1") {
    throw new Error(`not a PredictRLM turn: ${part.format}`);
  }
  // The turn's last tool calls are its predict calls, in the order of their details.
  const toolCount = turn.tool_calls.length - part.predict_calls.length;
  const predictCalls = turn.tool_calls.slice(toolCount);
  const omissions = native.omissions;
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
  };
}

function summary(terminal: AgentTraceTerminal): TraceBodySummary | undefined {
  if (terminal.kind !== "trace_finished") return undefined;
  const { native } = terminal.record;
  if (native.format !== "predict_rlm.run/v1") {
    throw new Error(`not a PredictRLM trace: ${native.format}`);
  }
  return {
    subModel: native.sub_model ?? undefined,
    maxIterations: native.max_iterations,
    usage: { main: tokenUsage(native.usage.main), sub: tokenUsage(native.usage.sub) },
  };
}

function retainedValue(event: AgentLiveEvent, kind: "inputs" | "outputs"): unknown {
  const { native } = event;
  if (kind === "inputs" && native.kind === "run.started") return native.inputs;
  if (kind === "outputs" && native.kind === "run.succeeded") return native.outputs;
  return undefined;
}

export const predictRlmViewer: TraceViewer = {
  turn,
  summary,
  // The sub-model, iteration limit and per-model usage are only in the final body.
  usageInBody: true,
  hasSubModel: true,
  turnUnit: "iterations",
  activityLabels: {
    "run.started": "Agent run started",
    "code.generated": "Generating code",
    "code.executed": "Running generated code",
    "predict.started": "Calling sub-model",
    "predict.finished": "Sub-model call finished",
    "tool.started": "Calling tool",
    "tool.finished": "Tool call finished",
    "run.succeeded": "Agent run completed",
    "run.failed": "Agent run failed",
    "run.cancelled": "Agent run cancelled",
  },
  valueEventKinds: { inputs: "run.started", outputs: "run.succeeded" },
  retainedValue,
};
