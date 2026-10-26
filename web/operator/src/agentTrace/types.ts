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

/** A sub-model call the agent's code made: its ``predict`` tool call and its details. */
export interface AgentTracePredictCall {
  signature: string;
  instructions?: string;
  model: string;
  durationMs?: number;
  usage: TraceTokenUsage;
  input: unknown;
  output: unknown;
  error?: string;
  lm?: TraceLmFinish;
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
  message?: string;
  /** The provider's own reasoning, when the runtime reports it. */
  reasoning?: string;
  /** Absent for runtimes that answer without running code. */
  execution?: AgentTraceExecution;
  error: boolean;
  /** Absent when the runtime does not time its turns. */
  durationMs?: number;
  /** Tool calls other than predict calls, as far as the live event kept them. */
  toolCalls: AgentTraceToolCall[];
  predictCalls: AgentTracePredictCall[];
  /** Both counts include calls a large turn's live event left out. */
  toolCallCount: number;
  predictCallCount: number;
  lm?: TraceLmFinish;
  usage: TraceTurnUsage;
}

export type AgentTraceStepSummary = Pick<
  AgentTraceStep,
  | "iteration"
  | "message"
  | "error"
  | "durationMs"
  | "lm"
  | "usage"
  | "toolCallCount"
  | "predictCallCount"
> & { hasExecution: boolean; hasReasoning: boolean };

export function summarizeAgentTraceStep(step: AgentTraceStep): AgentTraceStepSummary {
  return {
    iteration: step.iteration,
    message: step.message,
    error: step.error,
    durationMs: step.durationMs,
    lm: step.lm,
    usage: step.usage,
    toolCallCount: step.toolCallCount,
    predictCallCount: step.predictCallCount,
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
