// What the trace explorer and inspector header show, whatever the runtime.

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

/** A turn's tokens; the cost is absent when the runtime prices only the whole run. */
export type TraceTurnTokens = Omit<TraceTokenUsage, "cost"> & { cost?: number };

export interface TraceTurnUsage {
  /** Absent when the runtime reported no usage for the turn. */
  main?: TraceTurnTokens;
  /** Absent for runtimes that have no sub-model, which also make no predict calls. */
  sub?: TraceTurnTokens;
}

export interface TraceLmFinish {
  finishReason?: string;
}

export interface AgentTraceToolCall {
  name: string;
  arguments: unknown;
  result: unknown;
  error?: string;
  durationMs?: number;
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
  /** Code the agent ran; absent for runtimes that answer without running code. */
  code?: string;
  /** What `output` holds: a code sandbox's output, or the model's reply. */
  outputKind: "sandbox" | "response";
  output: string;
  untruncatedOutput: string;
  error: boolean;
  /** Absent when the runtime does not time its turns. */
  durationMs?: number;
  toolCalls: AgentTraceToolCall[];
  predictCalls: AgentTracePredictGroup[];
  lm?: TraceLmFinish;
  usage: TraceTurnUsage;
}

export type AgentTraceStepSummary = Pick<
  AgentTraceStep,
  "iteration" | "reasoning" | "error" | "durationMs" | "lm" | "usage" | "outputKind"
>;

export function summarizeAgentTraceStep(step: AgentTraceStep): AgentTraceStepSummary {
  return {
    iteration: step.iteration,
    reasoning: step.reasoning,
    error: step.error,
    durationMs: step.durationMs,
    lm: step.lm,
    usage: step.usage,
    outputKind: step.outputKind,
  };
}

/** Details a runtime keeps in its final trace body rather than the common header. */
export interface TraceBodySummary {
  subModel?: string;
  maxIterations: number;
  usage: TraceLmUsage;
}
