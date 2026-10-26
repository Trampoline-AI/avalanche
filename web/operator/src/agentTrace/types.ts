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

/** Code the agent wrote and ran in a turn, with what it printed. */
export interface AgentTraceExecution {
  code: string;
  output: string;
  untruncatedOutput: string;
}

export interface AgentTraceStep {
  iteration: number;
  /** What the model wrote this turn; absent when it wrote nothing. */
  text?: string;
  /** The provider's own reasoning, when the runtime reports it. */
  reasoning?: string;
  /** Absent for runtimes that answer without running code. */
  execution?: AgentTraceExecution;
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
  "iteration" | "text" | "error" | "durationMs" | "lm" | "usage"
> & { hasExecution: boolean; hasReasoning: boolean };

export function summarizeAgentTraceStep(step: AgentTraceStep): AgentTraceStepSummary {
  return {
    iteration: step.iteration,
    text: step.text,
    error: step.error,
    durationMs: step.durationMs,
    lm: step.lm,
    usage: step.usage,
    hasExecution: step.execution !== undefined,
    hasReasoning: step.reasoning !== undefined,
  };
}

/** Details a runtime keeps in its final trace body rather than the common header. */
export interface TraceBodySummary {
  subModel?: string;
  maxIterations: number;
  usage: TraceLmUsage;
}
