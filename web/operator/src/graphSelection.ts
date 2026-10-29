import { MarkerType, type Edge } from "@xyflow/react";

export function highlightConnectedEdges(
  edges: readonly Edge[],
  selectedNodeId?: string,
): Edge[] {
  return edges.map((edge) => {
    const connected =
      selectedNodeId !== undefined &&
      (edge.source === selectedNodeId || edge.target === selectedNodeId);
    const muted = selectedNodeId !== undefined && !connected;
    const color = connected ? "var(--color-acid)" : "var(--color-edge)";
    return {
      ...edge,
      className: `${edge.className ?? ""}${connected ? " dag-edge--connected" : muted ? " dag-edge--muted" : ""}`,
      ariaLabel: `${edge.source} to ${edge.target}${connected ? " (connected to selected node)" : ""}`,
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
