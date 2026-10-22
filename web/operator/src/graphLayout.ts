import { MarkerType, type Edge } from "@xyflow/react";
import type { WorkflowTopologyMsg } from "./model";
import type { GraphOrientation } from "./useGraphOrientation";

interface GraphLayout {
  edges: Edge[];
  rows: Record<number, string[]>;
}

export function createGraphLayout(
  topology: Pick<WorkflowTopologyMsg, "nodeIds" | "graph">,
  orientation: GraphOrientation,
): GraphLayout {
  const incoming = Object.fromEntries(topology.nodeIds.map((nodeId) => [nodeId, 0]));
  for (const edges of Object.values(topology.graph)) {
    for (const child of edges.children) incoming[child] = (incoming[child] ?? 0) + 1;
  }
  const depths: Record<string, number> = {};
  const roots = topology.nodeIds.filter((nodeId) => incoming[nodeId] === 0);
  const queue = [...roots];
  for (const nodeId of roots) depths[nodeId] = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const parent = queue[index];
    for (const child of topology.graph[parent]?.children ?? []) {
      depths[child] = Math.max(depths[child] ?? 0, depths[parent] + 1);
      incoming[child] -= 1;
      if (incoming[child] === 0) queue.push(child);
    }
  }
  // Independent inputs belong beside the work that prepares their first consumer,
  // rather than at the top with a long connection past unrelated earlier steps.
  // These are presentation ranks only; the workflow's dependencies never change.
  const deferredInputs = new Set<string>();
  for (const root of roots) {
    const consumers = topology.graph[root]?.children ?? [];
    if (consumers.length === 0) continue;
    depths[root] = Math.min(...consumers.map((id) => depths[id])) - 1;
    if (depths[root] > 0) deferredInputs.add(root);
  }
  const rows: Record<number, string[]> = {};
  // Keep the existing preparation steps first and place supporting inputs beside them.
  const layoutNodeIds = [
    ...topology.nodeIds.filter((id) => !deferredInputs.has(id)),
    ...deferredInputs,
  ];
  for (const nodeId of layoutNodeIds) {
    const depth = depths[nodeId] ?? 0;
    (rows[depth] ??= []).push(nodeId);
  }
  const seen = new Set<string>();
  const edges: Edge[] = [];
  let skipEdgeLane = 0;
  for (const [source, children] of Object.entries(topology.graph)) {
    // Only long connections use the outer trunk. Adjacent destinations stay
    // local, sharing the same junction in the gap after the source row.
    const usesOuterTrunk = children.children.some(
      (target) => depths[target] !== (depths[source] ?? 0) + 1,
    );
    const lane = usesOuterTrunk ? skipEdgeLane++ : undefined;
    const branches = new Set(children.children).size > 1;
    for (const target of children.children) {
      const id = `${source}->${target}`;
      if (seen.has(id)) continue;
      seen.add(id);
      const isSkipEdge = depths[target] !== (depths[source] ?? 0) + 1;
      edges.push({
        id,
        source,
        target,
        sourceHandle: orientation === "vertical" ? "source-bottom" : "source-right",
        targetHandle: orientation === "vertical" ? "target-top" : "target-left",
        markerEnd: { type: MarkerType.ArrowClosed },
        type: isSkipEdge ? "skip" : branches ? "branch" : "step",
        data: isSkipEdge
          ? {
              lane,
              orientation,
              sourceRowIds: rows[depths[source] ?? 0],
              nextRowIds: rows[(depths[source] ?? 0) + 1],
              targetRowIds: rows[depths[target] ?? 0],
              intermediateRows: Array.from(
                { length: depths[target] - depths[source] - 1 },
                (_, index) => rows[depths[source] + index + 1],
              ),
            }
          : branches
            ? {
                orientation,
                sourceRowIds: rows[depths[source] ?? 0],
                targetRowIds: rows[depths[target] ?? 0],
              }
            : undefined,
        className: "dag-edge",
      });
    }
  }
  return { edges, rows };
}

export function graphPositions(
  rows: Record<number, string[]>,
  measurements: ReadonlyMap<string, { width: number; height: number }>,
  orientation: GraphOrientation,
): Record<string, { x: number; y: number }> {
  if (orientation === "vertical") {
    // Origins are at the left edge and vertical center of each card in both layouts.
    const positions: Record<string, { x: number; y: number }> = {};
    let top = 0;
    for (const nodeIds of Object.values(rows)) {
      const heights = nodeIds.map((id) => measurements.get(id)?.height ?? 130);
      const widths = nodeIds.map((id) => measurements.get(id)?.width ?? 360);
      const height = Math.max(...heights);
      const width = widths.reduce((sum, value) => sum + value, 0) + (nodeIds.length - 1) * 60;
      let left = -width / 2;
      nodeIds.forEach((id, index) => {
        positions[id] = { x: left, y: top + height / 2 };
        left += widths[index] + 60;
      });
      top += height + 100;
    }
    return positions;
  }

  // Share row spacing across columns so aligned neighbors keep straight edges.
  let rowSpacing = 220;
  for (const nodeIds of Object.values(rows)) {
    for (let row = 1; row < nodeIds.length; row += 1) {
      const above = measurements.get(nodeIds[row - 1])?.height ?? 130;
      const below = measurements.get(nodeIds[row])?.height ?? 130;
      rowSpacing = Math.max(rowSpacing, (above + below) / 2 + 40);
    }
  }
  return Object.fromEntries(
    Object.entries(rows).flatMap(([depth, nodeIds]) =>
      nodeIds.map((id, row) => [
        id,
        { x: Number(depth) * 500, y: (row - (nodeIds.length - 1) / 2) * rowSpacing },
      ]),
    ),
  );
}
