import type { MachineSpec } from "./machine";
import { DEFAULT_NODE_SIZE } from "./graphLayout";
import type { GraphOrientation } from "./useGraphOrientation";

/** The machine mode of GraphCanvas: states are ordinary node cards, arcs may cycle. */
export interface MachineGraphView {
  spec: MachineSpec;
  /** Current state of the displayed run; omitted for a definition. */
  state?: string | null;
  /** Run status, shown on the current state's card. */
  status?: string;
  /** Absolute Unix seconds when the current state's timer fires. */
  due?: number | null;
  /** Opens a state's latest visit; omitted for a definition. */
  onOpenState?: (state: string) => void;
}

export type MachineArcKind = "forward" | "back" | "self";

export interface MachineArcData extends Record<string, unknown> {
  kind: MachineArcKind;
  orientation: GraphOrientation;
  /** Index among arcs between the same pair of states, so parallel arcs fan out. */
  lane: number;
  label: string;
  timer?: { delaySeconds: number; due?: number };
  /** The arc leaves the current state, so the run can take it now. */
  live: boolean;
}

export interface MachineLink {
  id: string;
  source: string;
  target: string;
  data: MachineArcData;
}

const COLUMN_GAP = { horizontal: 220, vertical: 120 };
const RANK_GAP = { horizontal: 200, vertical: 190 };

/**
 * Ranks states by breadth-first distance from the initial state, so a cycle never
 * reaches the DAG layer assignment. Arcs to the same or an earlier rank curve
 * around the cards (back arcs and self-loops) instead of crossing them.
 */
export function layoutMachine(
  view: MachineGraphView,
  orientation: GraphOrientation,
): {
  ranks: Map<string, number>;
  positions: Record<string, { x: number; y: number }>;
  links: MachineLink[];
} {
  const { spec } = view;
  const links = Object.entries(spec.states).flatMap(([source, declaration]) => [
    ...declaration.transitions.map((transition) => ({
      source,
      target: transition.target,
      label: transition.event,
      timer: undefined as MachineArcData["timer"],
    })),
    ...declaration.timers.map((timer) => ({
      source,
      target: timer.target,
      label: `after ${formatDelay(timer.delay_seconds)}`,
      timer: {
        delaySeconds: timer.delay_seconds,
        due: source === view.state && view.due != null ? view.due : undefined,
      },
    })),
  ]);
  const ranks = new Map<string, number>([[spec.initial, 0]]);
  const queue = [spec.initial];
  for (let index = 0; index < queue.length; index++) {
    for (const link of links) {
      if (link.source === queue[index] && !ranks.has(link.target)) {
        ranks.set(link.target, ranks.get(link.source)! + 1);
        queue.push(link.target);
      }
    }
  }
  // Unreachable states (never entered from the initial state) follow the last rank.
  const lastRank = Math.max(0, ...ranks.values());
  for (const name of Object.keys(spec.states))
    if (!ranks.has(name)) ranks.set(name, lastRank + 1);
  const byRank = new Map<number, string[]>();
  for (const name of Object.keys(spec.states)) {
    const rank = ranks.get(name)!;
    byRank.set(rank, [...(byRank.get(rank) ?? []), name]);
  }
  const { width, height } = DEFAULT_NODE_SIZE;
  const positions: Record<string, { x: number; y: number }> = {};
  for (const [rank, names] of byRank) {
    names.forEach((name, row) => {
      const offset = row - (names.length - 1) / 2;
      // GraphCanvas anchors nodes at their left edge, vertically centred.
      positions[name] =
        orientation === "horizontal"
          ? {
              x: rank * (width + RANK_GAP.horizontal),
              y: offset * (height + COLUMN_GAP.horizontal),
            }
          : {
              x: offset * (width + COLUMN_GAP.vertical) - width / 2,
              y: rank * (height + RANK_GAP.vertical),
            };
    });
  }
  const lanes = new Map<string, number>();
  return {
    ranks,
    positions,
    links: links.map((link, index) => {
      const pair = JSON.stringify([link.source, link.target]);
      const lane = lanes.get(pair) ?? 0;
      lanes.set(pair, lane + 1);
      const kind: MachineArcKind =
        link.source === link.target
          ? "self"
          : ranks.get(link.target)! <= ranks.get(link.source)!
            ? "back"
            : "forward";
      return {
        id: `machine-arc-${index}`,
        source: link.source,
        target: link.target,
        data: {
          kind,
          orientation,
          lane,
          label: link.label,
          timer: link.timer,
          live: link.source === view.state,
        },
      };
    }),
  };
}

export function formatDelay(seconds: number): string {
  if (seconds % 86_400 === 0) return `${seconds / 86_400}d`;
  if (seconds % 3_600 === 0) return `${seconds / 3_600}h`;
  if (seconds % 60 === 0 && seconds >= 60) return `${seconds / 60}m`;
  return `${seconds}s`;
}

type Point = { x: number; y: number };

function cubic(points: [Point, Point, Point, Point]): { path: string; label: Point } {
  const [a, b, c, d] = points;
  return {
    path: `M ${a.x},${a.y} C ${b.x},${b.y} ${c.x},${c.y} ${d.x},${d.y}`,
    // The curve's point at t = 0.5.
    label: { x: (a.x + 3 * b.x + 3 * c.x + d.x) / 8, y: (a.y + 3 * b.y + 3 * c.y + d.y) / 8 },
  };
}

/** Cubic arc geometry for a machine edge between two handle points. */
export function machineArcPath(
  data: Pick<MachineArcData, "kind" | "orientation" | "lane">,
  source: Point,
  target: Point,
): { path: string; label: Point } {
  const horizontal = data.orientation === "horizontal";
  // Parallel arcs fan out alternately on either side of the straight line.
  const fan = (data.lane % 2 === 0 ? 1 : -1) * Math.ceil(data.lane / 2) * 46;
  if (data.kind === "forward") {
    if (horizontal) {
      const pull = Math.max(70, Math.abs(target.x - source.x) / 2);
      return cubic([
        source,
        { x: source.x + pull, y: source.y + fan },
        { x: target.x - pull, y: target.y + fan },
        target,
      ]);
    }
    const pull = Math.max(60, Math.abs(target.y - source.y) / 2);
    return cubic([
      source,
      { x: source.x + fan, y: source.y + pull },
      { x: target.x + fan, y: target.y - pull },
      target,
    ]);
  }
  if (data.kind === "self") {
    // From the card's outgoing side round its corner into the adjacent side, clear of
    // forward arcs (which use the opposite sides) and back arcs (which bow away).
    const reach = 110 + data.lane * 44;
    return horizontal
      ? cubic([
          source,
          { x: source.x + reach, y: source.y },
          { x: target.x, y: target.y - reach },
          target,
        ])
      : cubic([
          source,
          { x: source.x, y: source.y + reach },
          { x: target.x - reach, y: target.y },
          target,
        ]);
  }
  // Back arcs leave and re-enter on the same side, bowing out further for longer jumps.
  const span = horizontal ? Math.abs(target.x - source.x) : Math.abs(target.y - source.y);
  const lift = 80 + data.lane * 40 + span * 0.07;
  return horizontal
    ? cubic([
        source,
        { x: source.x, y: Math.max(source.y, target.y) + lift },
        { x: target.x, y: Math.max(source.y, target.y) + lift },
        target,
      ])
    : cubic([
        source,
        { x: Math.max(source.x, target.x) + lift, y: source.y },
        { x: Math.max(source.x, target.x) + lift, y: target.y },
        target,
      ]);
}

/** Handles on the shared node card that each arc kind attaches to. */
export function machineArcHandles(
  kind: MachineArcKind,
  orientation: GraphOrientation,
): { sourceHandle: string; targetHandle: string } {
  if (kind === "forward")
    return orientation === "horizontal"
      ? { sourceHandle: "source-right", targetHandle: "target-left" }
      : { sourceHandle: "source-bottom", targetHandle: "target-top" };
  if (kind === "self")
    return orientation === "horizontal"
      ? { sourceHandle: "source-right", targetHandle: "target-top" }
      : { sourceHandle: "source-bottom", targetHandle: "target-left" };
  return orientation === "horizontal"
    ? { sourceHandle: "source-bottom", targetHandle: "target-bottom" }
    : { sourceHandle: "source-right", targetHandle: "target-right" };
}
