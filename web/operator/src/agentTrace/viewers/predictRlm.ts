import type { PredictRLMTokenUsage } from "../../generated/agentSchemas";
import type { AgentLiveEvent, AgentTraceTerminal } from "../common";
import type { AgentTraceStep, TraceBodySummary, TraceTokenUsage } from "../types";
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
  const codes = turn.actions.filter((action) => action.kind === "code_execution");
  const code = codes[0];
  if (codes.length !== 1 || code === undefined) {
    throw new Error("a PredictRLM turn runs exactly one code block");
  }
  return {
    iteration: turn.index,
    reasoning: turn.reasoning ?? "",
    code: code.code,
    output: code.output,
    untruncatedOutput: native.untruncated_output,
    // Only failed code fails a turn; the code may have handled a failed tool call.
    error: code.error !== null,
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
    predictCalls: native.predict_calls.map((group) => ({
      signature: group.signature,
      instructions: group.instructions ?? undefined,
      model: group.model,
      totalUsage: tokenUsage(group.total_usage),
      calls: group.calls.map((call) => ({
        durationMs: call.duration_ms,
        usage: tokenUsage(call.usage),
        input: call.input,
        output: call.output,
        error: call.error ?? undefined,
        lm: { finishReason: call.finish_reason ?? undefined },
      })),
    })),
    lm: { finishReason: native.finish_reason ?? undefined },
    usage: { main: tokenUsage(native.usage.main), sub: tokenUsage(native.usage.sub) },
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
