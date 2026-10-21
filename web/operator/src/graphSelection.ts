import { MarkerType, type Edge } from "@xyflow/react";

// Follow each direction independently so sibling branches stay neutral.
function connectedEdges(edges: readonly Edge[], nodeId: string, upstream: boolean) {
  const adjacency = new Map<string, Edge[]>();
  for (const edge of edges) {
    const key = upstream ? edge.target : edge.source;
    const neighbors = adjacency.get(key) ?? [];
    neighbors.push(edge);
    adjacency.set(key, neighbors);
  }
  const visited = new Set([nodeId]);
  const connected = new Set<string>();
  const queue = [nodeId];
  for (let index = 0; index < queue.length; index += 1) {
    for (const edge of adjacency.get(queue[index]) ?? []) {
      connected.add(edge.id);
      const neighbor = upstream ? edge.source : edge.target;
      if (!visited.has(neighbor)) {
        visited.add(neighbor);
        queue.push(neighbor);
      }
    }
  }
  return connected;
}

export function highlightConnectedEdges(
  edges: readonly Edge[],
  selectedNodeId?: string,
): Edge[] {
  const upstream = selectedNodeId
    ? connectedEdges(edges, selectedNodeId, true)
    : new Set<string>();
  const downstream = selectedNodeId
    ? connectedEdges(edges, selectedNodeId, false)
    : new Set<string>();
  return edges.map((edge) => {
    const direction = upstream.has(edge.id)
      ? "upstream"
      : downstream.has(edge.id)
        ? "downstream"
        : undefined;
    const color =
      direction === "upstream" ? "#6366f1" : direction === "downstream" ? "#0d9488" : "#87938d";
    return {
      ...edge,
      className: `${edge.className ?? ""}${direction ? ` dag-edge--${direction}` : ""}`,
      ariaLabel: `${edge.source} to ${edge.target}${direction ? ` (${direction})` : ""}`,
      style: { ...edge.style, stroke: color, strokeWidth: direction ? 2.8 : 1.4 },
      markerEnd: { type: MarkerType.ArrowClosed, color },
      zIndex: direction ? 1 : 0,
    };
  });
}
