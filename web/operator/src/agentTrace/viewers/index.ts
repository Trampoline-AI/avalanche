// One viewer per trace format. A format the generated schemas declare without a
// viewer here fails the TypeScript build.
import type { AgentLiveEvent, AgentTraceTerminal, NativeEventFormat } from "../common";
import type { AgentTraceStep, TraceBodySummary } from "../types";
import { claudeAgentViewer } from "./claudeAgent";
import { predictRlmViewer } from "./predictRlm";

export interface TraceViewer {
  /** One turn.finished event as the explorer shows it. */
  turn(event: AgentLiveEvent): AgentTraceStep;
  /** Header details kept in the final trace body; absent when the runtime keeps none. */
  summary?: (terminal: AgentTraceTerminal) => TraceBodySummary | undefined;
  /** Whether the header must wait for the final body for its token usage. */
  usageInBody: boolean;
  /** Whether the runtime reports a second, sub-model next to the main model. */
  hasSubModel: boolean;
  /** What the runtime calls a turn in counts, such as "iterations". */
  turnUnit: string;
  activityLabels: Record<string, string>;
  /** The native events that carry the run's inputs and outputs. */
  valueEventKinds: Record<"inputs" | "outputs", string>;
  retainedValue(event: AgentLiveEvent, kind: "inputs" | "outputs"): unknown;
}

export const TRACE_VIEWERS = {
  "predict_rlm.run/v1": predictRlmViewer,
  "claude_agent.messages/v1": claudeAgentViewer,
} satisfies Record<NativeEventFormat, TraceViewer>;

function isNativeEventFormat(format: string): format is NativeEventFormat {
  return Object.hasOwn(TRACE_VIEWERS, format);
}

/**
 * The viewer for a header's trace format, if this UI has one. The header itself is
 * runtime-neutral; a viewer only adds details the runtime keeps in its body.
 */
export function headerViewer(format: string): TraceViewer | undefined {
  return isNativeEventFormat(format) ? TRACE_VIEWERS[format] : undefined;
}

export function viewerFor(format: string): TraceViewer {
  if (!isNativeEventFormat(format)) {
    throw new Error(`No viewer for agent trace format ${format}`);
  }
  return TRACE_VIEWERS[format];
}

/** A live event's label, from whichever viewer names that native kind. */
export function activityLabel(nativeEventKind: string): string {
  for (const viewer of Object.values(TRACE_VIEWERS)) {
    const label = viewer.activityLabels[nativeEventKind];
    if (label !== undefined) return label;
  }
  return nativeEventKind;
}

/** Native event kinds that carry a run's inputs or outputs, across runtimes. */
export function valueEventKinds(kind: "inputs" | "outputs"): Set<string> {
  return new Set(Object.values(TRACE_VIEWERS).map((viewer) => viewer.valueEventKinds[kind]));
}
