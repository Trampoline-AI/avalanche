import type { GraphOrientation } from "./useGraphOrientation";

interface EdgeEndpoints {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
}

// All outgoing paths branch at the same midpoint in the gap between rows.
export function branchJunction(sourceRowEnd: number, nextRowStart: number): number {
  return (sourceRowEnd + nextRowStart) / 2;
}

export function branchEdgePath(
  { sourceX, sourceY, targetX, targetY }: EdgeEndpoints,
  orientation: GraphOrientation,
  junction: number,
): string {
  if (orientation === "vertical" ? sourceX === targetX : sourceY === targetY) {
    return `M ${sourceX},${sourceY} L ${targetX},${targetY}`;
  }
  return orientation === "vertical"
    ? `M ${sourceX},${sourceY} L ${sourceX},${junction} L ${targetX},${junction} L ${targetX},${targetY}`
    : `M ${sourceX},${sourceY} L ${junction},${sourceY} L ${junction},${targetY} L ${targetX},${targetY}`;
}

export function outerTrunkPath(
  { sourceX, sourceY, targetX, targetY }: EdgeEndpoints,
  orientation: GraphOrientation,
  graphExtent: number,
  lane: number,
  junction: number,
  targetRowStart: number,
): string {
  const route = graphExtent + 56 + lane * 32;
  // Approach through the gap before the destination row, never through siblings.
  const entry = targetRowStart - 28;
  return orientation === "vertical"
    ? `M ${sourceX},${sourceY} L ${sourceX},${junction} L ${route},${junction} L ${route},${entry} L ${targetX},${entry} L ${targetX},${targetY}`
    : `M ${sourceX},${sourceY} L ${junction},${sourceY} L ${junction},${route} L ${entry},${route} L ${entry},${targetY} L ${targetX},${targetY}`;
}
