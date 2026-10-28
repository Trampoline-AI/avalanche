// Runtime-neutral agent bodies as the operator serves them. The generated types
// check every fixture against the Python models.
import type {
  AgentLiveEvent,
  AgentTraceTerminal,
  JsonValue,
  PredictRLMTokenUsage,
  PredictRLMTraceHeader,
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
    ...rest,
  };
}

export interface TurnOptions {
  message?: string;
  reasoning?: string;
  code?: string;
  output?: string;
  untruncatedOutput?: string;
  failed?: boolean;
}

/** A PredictRLM turn with one tool call and one sub-model (predict) call. */
export function turnEvent(iteration: number, options: TurnOptions = {}): AgentLiveEvent {
  const usage = tokenUsage();
  return live(iteration, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      turn: {
        index: iteration,
        message: options.message ?? `Text for turn ${iteration}.`,
        reasoning: options.reasoning ?? null,
        duration_ms: 1,
        usage: {
          input_tokens: 2,
          output_tokens: 2,
          cost_usd: 0,
          cache_read_input_tokens: null,
          cache_creation_input_tokens: null,
        },
        tool_calls: [
          {
            call_id: null,
            caller: "code",
            name: `lookup_${iteration}`,
            arguments: { args: [], kwargs: {} },
            result: `Tool result ${iteration}`,
            error: null,
            duration_ms: 1,
          },
          {
            call_id: null,
            caller: "code",
            name: "predict",
            arguments: { question: "Ready?" },
            result: { answer: `Prediction ${iteration}` },
            error: null,
            duration_ms: 1,
          },
        ],
        native: {
          format: "predict_rlm.run/v1",
          code: options.code ?? `print("turn ${iteration}")`,
          output: options.output ?? `Output for turn ${iteration}.`,
          untruncated_output: options.untruncatedOutput ?? `Full output for turn ${iteration}.`,
          error: options.failed ?? false,
          finish_reason: "stop",
          usage: { main: usage, sub: usage },
          predict_calls: [
            {
              signature: "question -> answer",
              instructions: null,
              model: "sub-model",
              usage,
              finish_reason: null,
            },
          ],
        },
      },
    },
    // A turn its live event kept whole has no native part.
    native: null,
  });
}

/** The common event every runtime starts a run with. */
export function runStartedEvent(
  inputs: Record<string, JsonValue>,
  sequence = 1,
  omitted: string[] = [],
): AgentLiveEvent {
  return live(sequence, {
    common: { kind: "run.started", model: "main-model", inputs, omitted_inputs: omitted },
    native: null,
  });
}

export interface HeaderOptions {
  subModel?: string | null;
  maxIterations?: number;
  main?: PredictRLMTokenUsage;
  sub?: PredictRLMTokenUsage;
}

/** PredictRLM's part of a trace header. */
export function predictRlmTraceHeader(options: HeaderOptions = {}): PredictRLMTraceHeader {
  return {
    format: "predict_rlm.run/v1",
    sub_model: options.subModel ?? null,
    max_iterations: options.maxIterations ?? 4,
    usage: { main: options.main ?? tokenUsage(), sub: options.sub ?? tokenUsage() },
    telemetry_ref: null,
  };
}

export interface FinishedOptions extends HeaderOptions {
  outputs?: Record<string, JsonValue> | null;
  omittedOutputs?: string[];
}

/** A PredictRLM final event: the header and the outputs; its turns travelled live. */
export function finishedTrace(options: FinishedOptions = {}): AgentTraceTerminal {
  return {
    kind: "trace_finished",
    schema_version: 2,
    invocation_id: "invocation-1",
    complete: true,
    outputs: options.outputs === undefined ? { answer: "done" } : options.outputs,
    omitted_outputs: options.omittedOutputs ?? [],
    header: {
      status: "succeeded",
      stop_reason: "submit",
      model: "retained-model",
      duration_ms: 125,
      usage: null,
      turn_count: 2,
      tool_call_count: 0,
      external_run_id: "run-1",
      session_id: null,
      native: predictRlmTraceHeader(options),
    },
  };
}

/** A Claude turn: a message, thinking and one tool call. */
export function claudeTurnEvent(index: number): AgentLiveEvent {
  return live(index, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      turn: {
        index,
        message: `Answer text ${index}.`,
        reasoning: `Thinking ${index}.`,
        duration_ms: null,
        usage: {
          input_tokens: 3,
          output_tokens: 5,
          cost_usd: null,
          cache_read_input_tokens: null,
          cache_creation_input_tokens: null,
        },
        tool_calls: [
          {
            call_id: `call-${index}`,
            caller: "model",
            name: "mcp__avalanche__lookup",
            arguments: { name: "a" },
            result: "10",
            error: null,
            duration_ms: null,
          },
        ],
        native: {
          format: "claude_agent.messages/v1",
          message_id: `msg-${index}`,
          model: "claude-haiku-5-5",
          stop_reason: "tool_use",
          api_error: null,
        },
      },
    },
    native: null,
  });
}

/** An OpenAI Agents SDK turn: a message, a reasoning summary and one tool call. */
export function openaiTurnEvent(index: number): AgentLiveEvent {
  return live(index, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      turn: {
        index,
        message: `Looking it up ${index}.`,
        reasoning: `Reasoning ${index}.`,
        duration_ms: 640,
        usage: {
          input_tokens: 30,
          output_tokens: 20,
          cost_usd: null,
          cache_read_input_tokens: 60,
          cache_creation_input_tokens: 10,
        },
        tool_calls: [
          {
            call_id: `call-${index}`,
            caller: "model",
            name: "lookup",
            arguments: { name: "a" },
            result: "10",
            error: null,
            duration_ms: 3,
          },
        ],
        native: {
          format: "openai_agents.responses/v1",
          response_id: `resp-${index}`,
          model: "gpt-5.4-mini",
          reasoning_tokens: 5,
          refusal: null,
          answer: false,
        },
      },
    },
    native: null,
  });
}

/** A Pydantic AI turn: a message, thinking, one tool call and the answer it hands back. */
export function pydanticAiTurnEvent(index: number): AgentLiveEvent {
  return live(index, {
    common: {
      kind: "turn.finished",
      omitted_tool_calls: 0,
      turn: {
        index,
        message: `Answer text ${index}.`,
        reasoning: `Thinking ${index}.`,
        duration_ms: 900,
        usage: {
          input_tokens: 3,
          output_tokens: 5,
          cost_usd: 0.0004,
          cache_read_input_tokens: 900,
          cache_creation_input_tokens: 0,
        },
        tool_calls: [
          {
            call_id: `call-${index}`,
            caller: "model",
            name: "lookup",
            arguments: { name: "a" },
            result: 10,
            error: null,
            duration_ms: 2,
          },
        ],
        native: {
          format: "pydantic_ai.messages/v1",
          model: "gpt-5.4-nano",
          provider: "openai",
          provider_response_id: `resp-${index}`,
          finish_reason: "tool_call",
        },
      },
    },
    native: null,
  });
}
