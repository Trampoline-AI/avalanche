import { MarkerType, type Edge } from "@xyflow/react";

import { INPUT_NODE_ID } from "./workflowInput";

/** SVG marker IDs for the diamond on edges that carry the run input (see GraphCanvas). */
export const INPUT_EDGE_MARKER = "ava-input-diamond";
export const INPUT_EDGE_MARKER_CONNECTED = "ava-input-diamond-connected";

export function highlightConnectedEdges(
  edges: readonly Edge[],
  selectedNodeId?: string,
): Edge[] {
  return edges.map((edge) => {
    const connected =
      selectedNodeId !== undefined &&
      (edge.source === selectedNodeId || edge.target === selectedNodeId);
    const muted = selectedNodeId !== undefined && !connected;
    const ariaLabel = `${edge.source} to ${edge.target}${connected ? " (connected to selected node)" : ""}`;
    const stateClass = connected ? " dag-edge--connected" : muted ? " dag-edge--muted" : "";
    if (edge.source === INPUT_NODE_ID) {
      // Input edges mean "this step is given the run input", not "runs after":
      // always dashed amber with a diamond head, and faded rather than re-dashed when muted.
      return {
        ...edge,
        className: `${edge.className ?? ""} dag-edge--input${stateClass}`,
        ariaLabel,
        style: {
          ...edge.style,
          stroke: connected ? "var(--color-acid)" : "var(--color-amber)",
          strokeWidth: connected ? 2.4 : 1.4,
          strokeDasharray: "5 4",
          opacity: muted ? 0.35 : undefined,
        },
        markerEnd: connected ? INPUT_EDGE_MARKER_CONNECTED : INPUT_EDGE_MARKER,
        zIndex: connected ? 1 : 0,
      };
    }
    const color = connected ? "var(--color-acid)" : "var(--color-edge)";
    return {
      ...edge,
      className: `${edge.className ?? ""}${stateClass}`,
      ariaLabel,
      style: {
        ...edge.style,
        stroke: color,
        strokeWidth: connected ? 2.8 : 1.4,
        strokeDasharray: muted ? "6 4" : undefined,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color },
      zIndex: connected ? 1 : 0,
    };
  });
}
