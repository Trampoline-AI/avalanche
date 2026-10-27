// Runtime-neutral agent trace bodies, validated with the schemas generated from
// the Python models. Every body is parsed here once and typed from then on.
import {
  AgentLiveEvent,
  AgentTraceHeader,
  AgentTraceTerminal,
  type TurnFinished,
} from "../generated/agentSchemas";

export type { AgentLiveEvent, AgentTraceTerminal, TurnFinished };

/** The trace formats the operator can send; each one has a viewer. */
export type NativeEventFormat = AgentTraceHeader["native"]["format"];

export function parseAgentLiveEvent(value: unknown): AgentLiveEvent {
  return AgentLiveEvent.parse(value);
}

export function parseAgentTraceTerminal(value: unknown): AgentTraceTerminal {
  return AgentTraceTerminal.parse(value);
}

export function parseTraceHeaderNative(value: unknown): AgentTraceHeader["native"] {
  return AgentTraceHeader.shape.native.parse(value);
}

/** A finished turn's event; its turn's own part names the trace format. */
export function finishedTurn(event: AgentLiveEvent): TurnFinished {
  if (event.common?.kind !== "turn.finished") {
    throw new Error("not a finished turn event");
  }
  return event.common;
}
