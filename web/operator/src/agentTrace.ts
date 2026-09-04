import { isUnknownRecord } from "./guards";

export interface TraceTokenUsage {
  inputTokens: number;
  outputTokens: number;
  cost: number;
  cacheHits: number;
}

export interface TraceLmUsage {
  main: TraceTokenUsage;
  sub: TraceTokenUsage;
}

export interface TraceLmFinish {
  finishReason?: string;
}

export interface AgentTraceToolCall {
  name: string;
  args: unknown[];
  kwargs: Record<string, unknown>;
  result: unknown;
  error?: string;
  durationMs: number;
}

export interface AgentTracePredictCall {
  durationMs: number;
  usage: TraceTokenUsage;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error?: string;
  lm?: TraceLmFinish;
}

export interface AgentTracePredictGroup {
  signature: string;
  instructions?: string;
  model: string;
  totalUsage: TraceTokenUsage;
  calls: AgentTracePredictCall[];
}

export interface AgentTraceStep {
  iteration: number;
  reasoning: string;
  code: string;
  output: string;
  untruncatedOutput: string;
  error: boolean;
  durationMs: number;
  toolCalls: AgentTraceToolCall[];
  predictCalls: AgentTracePredictGroup[];
  lm?: TraceLmFinish;
  usage: TraceLmUsage;
}

export type AgentTraceStepSummary = Pick<
  AgentTraceStep,
  "iteration" | "reasoning" | "error" | "durationMs" | "lm" | "usage"
>;

export function summarizeAgentTraceStep(step: AgentTraceStep): AgentTraceStepSummary {
  return {
    iteration: step.iteration,
    reasoning: step.reasoning,
    error: step.error,
    durationMs: step.durationMs,
    lm: step.lm,
    usage: step.usage,
  };
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!isUnknownRecord(value)) throw new Error(`${path} must be an object`);
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string") throw new Error(`${path} must be a string`);
  return value;
}

function optionalString(value: unknown, path: string): string | undefined {
  if (value === null || value === undefined) return undefined;
  return string(value, path);
}

function nonNegativeNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${path} must be a non-negative number`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, path: string): number {
  const parsed = nonNegativeNumber(value, path);
  if (!Number.isInteger(parsed)) throw new Error(`${path} must be an integer`);
  return parsed;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${path} must be a boolean`);
  return value;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function parseTokenUsage(value: unknown, path: string): TraceTokenUsage {
  const usage = record(value, path);
  return {
    inputTokens: nonNegativeInteger(usage.input_tokens, `${path}.input_tokens`),
    outputTokens: nonNegativeInteger(usage.output_tokens, `${path}.output_tokens`),
    cost: nonNegativeNumber(usage.cost, `${path}.cost`),
    cacheHits: nonNegativeInteger(usage.cache_hits, `${path}.cache_hits`),
  };
}

function parseLmUsage(value: unknown, path: string): TraceLmUsage {
  const usage = record(value, path);
  return {
    main: parseTokenUsage(usage.main, `${path}.main`),
    sub: parseTokenUsage(usage.sub, `${path}.sub`),
  };
}

export function parseAgentTraceUsage(value: unknown): TraceLmUsage {
  return parseLmUsage(value, "agent trace usage");
}

function parseLmFinish(value: unknown, path: string): TraceLmFinish | undefined {
  if (value === null || value === undefined) return undefined;
  const lm = record(value, path);
  return { finishReason: optionalString(lm.finish_reason, `${path}.finish_reason`) };
}

function parseToolCall(value: unknown, index: number): AgentTraceToolCall {
  const path = `trace step tool_calls[${index}]`;
  const call = record(value, path);
  return {
    name: string(call.name, `${path}.name`),
    args: array(call.args, `${path}.args`),
    kwargs: record(call.kwargs, `${path}.kwargs`),
    result: call.result,
    error: optionalString(call.error, `${path}.error`),
    durationMs: nonNegativeInteger(call.duration_ms, `${path}.duration_ms`),
  };
}

function parsePredictCall(
  value: unknown,
  groupIndex: number,
  callIndex: number,
): AgentTracePredictCall {
  const path = `trace step predict_calls[${groupIndex}].calls[${callIndex}]`;
  const call = record(value, path);
  return {
    durationMs: nonNegativeInteger(call.duration_ms, `${path}.duration_ms`),
    usage: parseTokenUsage(call.usage, `${path}.usage`),
    input: record(call.input, `${path}.input`),
    output: record(call.output, `${path}.output`),
    error: optionalString(call.error, `${path}.error`),
    lm: parseLmFinish(call.lm, `${path}.lm`),
  };
}

function parsePredictGroup(value: unknown, index: number): AgentTracePredictGroup {
  const path = `trace step predict_calls[${index}]`;
  const group = record(value, path);
  return {
    signature: string(group.signature, `${path}.signature`),
    instructions: optionalString(group.instructions, `${path}.instructions`),
    model: string(group.model, `${path}.model`),
    totalUsage: parseTokenUsage(group.total_usage, `${path}.total_usage`),
    calls: array(group.calls, `${path}.calls`).map((call, callIndex) =>
      parsePredictCall(call, index, callIndex),
    ),
  };
}

export function parseAgentTraceStepEvent(value: unknown): AgentTraceStep {
  const event = record(value, "agent trace event");
  if (string(event.event_kind, "agent trace event.event_kind") !== "iteration.recorded") {
    throw new Error("agent trace event must be an iteration.recorded event");
  }
  const data = record(event.data, "agent trace event.data");
  const step = record(data.step, "agent trace event.data.step");
  const iteration = nonNegativeInteger(step.iteration, "trace step.iteration");
  if (iteration < 1) throw new Error("trace step.iteration must be at least 1");

  return {
    iteration,
    reasoning: string(step.reasoning, "trace step.reasoning"),
    code: string(step.code, "trace step.code"),
    output: string(step.output, "trace step.output"),
    untruncatedOutput: string(step.untruncated_output, "trace step.untruncated_output"),
    error: boolean(step.error, "trace step.error"),
    durationMs: nonNegativeInteger(step.duration_ms, "trace step.duration_ms"),
    toolCalls: array(step.tool_calls, "trace step.tool_calls").map(parseToolCall),
    predictCalls: array(step.predict_calls, "trace step.predict_calls").map(parsePredictGroup),
    lm: parseLmFinish(step.lm, "trace step.lm"),
    usage: parseLmUsage(step.usage, "trace step.usage"),
  };
}
