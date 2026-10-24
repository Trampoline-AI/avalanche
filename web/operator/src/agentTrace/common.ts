// Runtime-neutral agent trace bodies, validated with the schemas generated from
// the Python models. Every body is parsed here once and typed from then on.
import {
  AgentLiveEvent,
  AgentTraceTerminal,
  type TurnFinished,
} from "../generated/agentSchemas";

export type { AgentLiveEvent, AgentTraceTerminal, TurnFinished };

/** The trace formats the operator can send; each one has a viewer. */
export type NativeEventFormat = AgentLiveEvent["native"]["format"];

export function parseAgentLiveEvent(value: unknown): AgentLiveEvent {
  return AgentLiveEvent.parse(value);
}

export function parseAgentTraceTerminal(value: unknown): AgentTraceTerminal {
  return AgentTraceTerminal.parse(value);
}
