// One viewer per trace format. A format the generated schemas declare without a
// viewer here fails the TypeScript build.
import type { AgentLiveEvent, NativeEventFormat } from "../common";
import { finishedTurn } from "../common";
import type { TraceHeaderMsg } from "../../model";
import type { AgentTraceStep, TraceHeaderLine } from "../types";
import { claudeAgentViewer } from "./claudeAgent";
import { predictRlmViewer } from "./predictRlm";

export interface TraceViewer {
  /** One turn.finished event as the explorer shows it. */
  turn(event: AgentLiveEvent): AgentTraceStep;
  /** What the runtime calls a turn in counts, such as "iterations". */
  turnUnit: string;
  /** Labels of the runtime's own (native) event kinds. */
  activityLabels: Record<string, string>;
  /** The run header's model, usage and turn lines, as this runtime describes them. */
  headerLines(header: TraceHeaderMsg): TraceHeaderLine[];
}

export const TRACE_VIEWERS = {
  "predict_rlm.run/v1": predictRlmViewer,
  "claude_agent.messages/v1": claudeAgentViewer,
} satisfies Record<NativeEventFormat, TraceViewer>;

// Events every runtime sends; an event without a native part is named by these.
const COMMON_LABELS: Record<string, string> = {
  "run.started": "Agent run started",
  "turn.finished": "Turn finished",
  "tool.started": "Calling tool",
  "tool.finished": "Tool call finished",
};

function isNativeEventFormat(format: string): format is NativeEventFormat {
  return Object.hasOwn(TRACE_VIEWERS, format);
}

export function viewerFor(format: string): TraceViewer {
  if (!isNativeEventFormat(format)) {
    throw new Error(`No viewer for agent trace format ${format}`);
  }
  return TRACE_VIEWERS[format];
}

/** The viewer for a finished turn's event, chosen by its turn's own format. */
export function turnViewer(event: AgentLiveEvent): TraceViewer {
  return viewerFor(finishedTurn(event).turn.native.format);
}

/**
 * A live event's label: its native kind as a viewer names it, else its common kind.
 * ``agentEventKind`` is "native" for an event with no common part.
 */
export function activityLabel(nativeEventKind: string, agentEventKind: string): string {
  if (nativeEventKind) {
    for (const viewer of Object.values(TRACE_VIEWERS)) {
      const label = viewer.activityLabels[nativeEventKind];
      if (label !== undefined) return label;
    }
    return nativeEventKind;
  }
  return COMMON_LABELS[agentEventKind] ?? agentEventKind;
}
