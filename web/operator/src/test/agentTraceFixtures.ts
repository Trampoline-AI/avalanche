// Runtime-neutral agent bodies as the operator serves them. The generated types
// check every fixture against the Python models.
import type {
  AgentLiveEvent,
  AgentTraceTerminal,
  JsonValue,
  PredictRLMTokenUsage,
} from "../generated/agentSchemas";

export function tokenUsage(
  input = 1,
  output = 1,
  cost = 0,
  cacheHits = 0,
): PredictRLMTokenUsage {
  return { input_tokens: input, output_tokens: output, cost, cache_hits: cacheHits };
}

function live(
  sequence: number,
  rest: Pick<AgentLiveEvent, "common" | "native">,
): AgentLiveEvent {
  return {
    kind: "event",
    schema_version: 2,
    invocation_id: "invocation-1",
    sequence,
    // A real time.time_ns() value, beyond JavaScript's safe integers.
    timestamp_ns: 1_792_855_720_829_187_328 + sequence * 1_024,
    runtime_kind: "predict_rlm",
    ...rest,
  };
}

export interface TurnOptions {
  reasoning?: string;
  code?: string;
  output?: string;
  untruncatedOutput?: string;
  failed?: boolean;
}

/** A PredictRLM turn with one tool call and one sub-model call. */
export function turnEvent(iteration: number, options: TurnOptions = {}): AgentLiveEvent {
  const usage = tokenUsage();
  const output = options.output ?? `Output for turn ${iteration}.`;
  return live(iteration, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      omitted_model_calls: 0,
      turn: {
        index: iteration,
        text: null,
        reasoning: options.reasoning ?? `Reasoning for turn ${iteration}.`,
        duration_ms: 1,
        usage: {
          input_tokens: 2,
          output_tokens: 2,
          cost_usd: 0,
          cache_read_input_tokens: null,
          cache_creation_input_tokens: null,
        },
        actions: [
          {
            kind: "code_execution",
            language: "python",
            code: options.code ?? `print("turn ${iteration}")`,
            output,
            error: options.failed ? output : null,
          },
          {
            kind: "tool_call",
            call_id: null,
            caller: "code",
            name: `lookup_${iteration}`,
            arguments: { args: [], kwargs: {} },
            result: `Tool result ${iteration}`,
            error: null,
            duration_ms: 1,
          },
          {
            kind: "model_call",
            call_id: null,
            caller: "code",
            model: "sub-model",
            duration_ms: 1,
            usage: null,
            error: null,
          },
        ],
      },
    },
    native: {
      format: "predict_rlm.run/v1",
      kind: "iteration.recorded",
      iteration,
      untruncated_output: options.untruncatedOutput ?? `Full output for turn ${iteration}.`,
      finish_reason: "stop",
      usage: { main: usage, sub: usage },
      predict_calls: [
        {
          signature: "question -> answer",
          instructions: null,
          model: "sub-model",
          total_usage: usage,
          calls: [
            {
              duration_ms: 1,
              usage,
              input: { question: "Ready?" },
              output: { answer: `Prediction ${iteration}` },
              error: null,
              finish_reason: null,
            },
          ],
        },
      ],
      tool_count: 1,
      predict_count: 1,
      omissions: null,
    },
  });
}

export function runStartedEvent(
  inputs: Record<string, JsonValue>,
  sequence = 1,
): AgentLiveEvent {
  return live(sequence, {
    common: null,
    native: {
      format: "predict_rlm.run/v1",
      kind: "run.started",
      input_fields: Object.keys(inputs),
      inputs,
    },
  });
}

export function runSucceededEvent(
  outputs: Record<string, JsonValue>,
  sequence = 2,
): AgentLiveEvent {
  return live(sequence, {
    common: null,
    native: {
      format: "predict_rlm.run/v1",
      kind: "run.succeeded",
      status: "completed",
      outputs,
    },
  });
}

export interface FinishedOptions {
  subModel?: string | null;
  maxIterations?: number;
  main?: PredictRLMTokenUsage;
  sub?: PredictRLMTokenUsage;
}

/** A PredictRLM final trace; its turns travelled as live events. */
export function finishedTrace(options: FinishedOptions = {}): AgentTraceTerminal {
  return {
    kind: "trace_finished",
    schema_version: 2,
    invocation_id: "invocation-1",
    complete: true,
    record: {
      schema_version: 2,
      turns_streamed: true,
      turns: [],
      header: {
        runtime_kind: "predict_rlm",
        trace_format: "predict_rlm.run/v1",
        status: "succeeded",
        stop_reason: "submit",
        model: "retained-model",
        duration_ms: 125,
        usage: null,
        turn_count: 2,
        tool_call_count: 0,
        external_run_id: "run-1",
        session_id: null,
      },
      native: {
        format: "predict_rlm.run/v1",
        sub_model: options.subModel ?? null,
        max_iterations: options.maxIterations ?? 4,
        usage: { main: options.main ?? tokenUsage(), sub: options.sub ?? tokenUsage() },
        telemetry_ref: null,
      },
    },
  };
}

function claudeLive(
  sequence: number,
  rest: Pick<AgentLiveEvent, "common" | "native">,
): AgentLiveEvent {
  return { ...live(sequence, rest), runtime_kind: "claude_agent" };
}

/** A Claude turn: text, thinking and one tool call. */
export function claudeTurnEvent(index: number): AgentLiveEvent {
  return claudeLive(index, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      omitted_model_calls: 0,
      turn: {
        index,
        text: `Answer text ${index}.`,
        reasoning: `Thinking ${index}.`,
        duration_ms: null,
        usage: {
          input_tokens: 3,
          output_tokens: 5,
          cost_usd: null,
          cache_read_input_tokens: null,
          cache_creation_input_tokens: null,
        },
        actions: [
          {
            kind: "tool_call",
            call_id: `call-${index}`,
            caller: "model",
            name: "mcp__avalanche__lookup",
            arguments: { name: "a" },
            result: "10",
            error: null,
            duration_ms: null,
          },
        ],
      },
    },
    native: {
      format: "claude_agent.messages/v1",
      kind: "message.finished",
      message_id: `msg-${index}`,
      model: "claude-haiku-5-5",
      stop_reason: "tool_use",
      api_error: null,
    },
  });
}

export function claudeStartedEvent(inputs: Record<string, JsonValue>): AgentLiveEvent {
  return claudeLive(1, {
    common: null,
    native: { format: "claude_agent.messages/v1", kind: "started", model: null, inputs },
  });
}

export function claudeFinishedEvent(output: Record<string, JsonValue> | null): AgentLiveEvent {
  return claudeLive(9, {
    common: null,
    native: {
      format: "claude_agent.messages/v1",
      kind: "finished",
      subtype: "success",
      is_error: false,
      num_turns: 2,
      structured_output: output,
      output_omitted: output === null,
      errors: [],
    },
  });
}
