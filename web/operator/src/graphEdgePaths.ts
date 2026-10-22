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
  rows: readonly TrunkRowBounds[],
  lane: number,
  junction: number,
  targetRowStart: number,
): string {
  const vertical = orientation === "vertical";
  const sourceAcross = vertical ? sourceX : sourceY;
  const targetAcross = vertical ? targetX : targetY;
  const targetForward = vertical ? targetY : targetX;
  const clearance = 24 + lane * 12;
  // Every edge from a source follows the same prefix. Widen the trunk only
  // before an intervening row requires it; nearer targets can peel off earlier.
  let route = Math.max(sourceAcross, (rows[0]?.extent ?? sourceAcross) + clearance);
  const point = (across: number, forward: number) =>
    vertical ? `${across},${forward}` : `${forward},${across}`;
  const path = [
    `M ${sourceX},${sourceY}`,
    `L ${point(sourceAcross, junction)}`,
    `L ${point(route, junction)}`,
  ];
  for (const row of rows.slice(1)) {
    const nextRoute = Math.max(route, row.extent + clearance);
    if (nextRoute === route) continue;
    path.push(`L ${point(route, row.start - 28)}`, `L ${point(nextRoute, row.start - 28)}`);
    route = nextRoute;
  }
  // Approach through the gap before the destination row, never through siblings.
  const entry = targetRowStart - 28;
  path.push(
    `L ${point(route, entry)}`,
    `L ${point(targetAcross, entry)}`,
    `L ${point(targetAcross, targetForward)}`,
  );
  return path.join(" ");
}

export interface TrunkRowBounds {
  start: number;
  extent: number;
}
