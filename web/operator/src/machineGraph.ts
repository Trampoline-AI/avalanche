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
  /** How many arcs join the same pair of states (one per trigger). */
  lanes?: number;
  /** Geometry routed around the state cards; absent for the input dot's edge. */
  route?: MachineArcRoute;
  label: string;
  timer?: { delaySeconds: number; due?: number };
  /** The arc leaves the current state, so the run can take it now. */
  live: boolean;
}

/** An arc's SVG path and where its label sits, clear of every state card. */
export interface MachineArcRoute {
  path: string;
  label: Point;
  /** Which side of `label` the label box extends to; centred when omitted. */
  anchor?: "start" | "end";
}

export interface MachineLink {
  id: string;
  source: string;
  target: string;
  data: MachineArcData;
}

// Wide enough between ranks for a forward arc's label, and between the states of
// one rank for a self-loop and its label, so no arc or label ever reaches a card.
const COLUMN_GAP = { horizontal: 260, vertical: 260 };
const RANK_GAP = { horizontal: 300, vertical: 260 };

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
  const pairCounts = new Map<string, number>();
  for (const link of links) {
    const pair = JSON.stringify([link.source, link.target]);
    pairCounts.set(pair, (pairCounts.get(pair) ?? 0) + 1);
  }
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
          lanes: pairCounts.get(pair),
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

/** A state card's bounding box in flow coordinates. */
export interface Box {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

function cubicAt([a, b, c, d]: readonly [Point, Point, Point, Point], t: number): Point {
  const u = 1 - t;
  const at = (key: "x" | "y") =>
    u * u * u * a[key] + 3 * u * u * t * b[key] + 3 * u * t * t * c[key] + t * t * t * d[key];
  return { x: at("x"), y: at("y") };
}

function cubic(points: [Point, Point, Point, Point], labelAt = 0.5): MachineArcRoute {
  const [a, b, c, d] = points;
  return {
    path: `M ${a.x},${a.y} C ${b.x},${b.y} ${c.x},${c.y} ${d.x},${d.y}`,
    label: cubicAt(points, labelAt),
  };
}

/** Forward cubic between two handle points (the input dot's edge into the initial state). */
export function machineArcPath(
  data: Pick<MachineArcData, "orientation">,
  source: Point,
  target: Point,
): MachineArcRoute {
  if (data.orientation === "horizontal") {
    const pull = Math.max(70, Math.abs(target.x - source.x) / 2);
    return cubic([
      source,
      { x: source.x + pull, y: source.y },
      { x: target.x - pull, y: target.y },
      target,
    ]);
  }
  const pull = Math.max(60, Math.abs(target.y - source.y) / 2);
  return cubic([
    source,
    { x: source.x, y: source.y + pull },
    { x: target.x, y: target.y - pull },
    target,
  ]);
}

// Clearances, in flow pixels, between routed arcs and the cards they pass.
/** Distance from the cards to the first back-arc lane; more than a self-loop and its label. */
const LANE_CLEARANCE = 72;
const LANE_SPACING = 30;
/** Where a back arc that cannot go straight out turns into the gap beside its rank. */
const DETOUR_STEP = 26;
const DETOUR_GAP = 40;
const CORNER = 18;
const SELF_LOOP_REACH = 40;
const SELF_LOOP_STEP = 24;
/** Parallel arcs between one pair of states attach this far apart on the card side. */
const PORT_SPREAD = 18;
/** Arc label box: 10 px monospace text plus padding and border (GraphCanvas `machine-arc-label`). */
const LABEL_CHAR = 6.3;
const LABEL_PADDING = 18;
const LABEL_HEIGHT = 20;
/**
 * Clear space between two stacked labels. Wider when labels stack across a horizontal
 * gap: a neighbouring arc starts to bend toward its target within the label's width.
 */
const LABEL_GAP = { horizontal: 20, vertical: 16 };

/** The rendered label box, estimated from its text (clock icon and countdown included). */
function labelSize(data: MachineArcData): { width: number; height: number } {
  let chars = data.label.length;
  // The clock icon, and a running countdown (`· 59m left`).
  if (data.timer) chars += 2;
  if (data.timer?.due !== undefined) chars += 12;
  return { width: chars * LABEL_CHAR + LABEL_PADDING, height: LABEL_HEIGHT };
}

/**
 * An orthogonal polyline as a path whose corners are short cubic curves.
 * Collinear and repeated points are dropped first.
 */
function roundedPath(points: readonly Point[]): string {
  const kept: Point[] = [];
  for (const point of points) {
    const last = kept.at(-1);
    if (last && Math.abs(last.x - point.x) < 0.01 && Math.abs(last.y - point.y) < 0.01)
      continue;
    const before = kept.at(-2);
    if (
      before &&
      last &&
      ((Math.abs(before.x - last.x) < 0.01 && Math.abs(last.x - point.x) < 0.01) ||
        (Math.abs(before.y - last.y) < 0.01 && Math.abs(last.y - point.y) < 0.01))
    )
      kept.pop();
    kept.push(point);
  }
  const round = (value: number) => Math.round(value * 10) / 10;
  const at = ({ x, y }: Point) => `${round(x)},${round(y)}`;
  let path = `M ${at(kept[0])}`;
  for (let index = 1; index < kept.length - 1; index++) {
    const [a, b, c] = [kept[index - 1], kept[index], kept[index + 1]];
    const inLength = Math.hypot(b.x - a.x, b.y - a.y);
    const outLength = Math.hypot(c.x - b.x, c.y - b.y);
    const radius = Math.min(CORNER, inLength / 2, outLength / 2);
    const enter = {
      x: b.x - ((b.x - a.x) / inLength) * radius,
      y: b.y - ((b.y - a.y) / inLength) * radius,
    };
    const leave = {
      x: b.x + ((c.x - b.x) / outLength) * radius,
      y: b.y + ((c.y - b.y) / outLength) * radius,
    };
    path += ` L ${at(enter)} C ${at(b)} ${at(b)} ${at(leave)}`;
  }
  return `${path} L ${at(kept.at(-1)!)}`;
}

/**
 * Routes every machine arc around the state cards, from their measured boxes.
 *
 * Forward arcs stay in the empty gap between two adjacent ranks. Back arcs run in a
 * lane outside every card of the ranks they span, on the outer side of their state
 * (detouring through the gap beside the rank when another card of the same rank is
 * in the way). Self-loops sit outside the card on the side away from its back
 * arcs. Each label lies on its own arc, in empty space.
 */
export function routeMachineArcs(
  links: readonly MachineLink[],
  boxes: Readonly<Record<string, Box>>,
  ranks: ReadonlyMap<string, number>,
  orientation: GraphOrientation,
): Record<string, MachineArcRoute> {
  const horizontal = orientation === "horizontal";
  // Work in (along, across): along runs with the ranks, across between a rank's states.
  type Span = { alongLow: number; alongHigh: number; acrossLow: number; acrossHigh: number };
  const spans: Record<string, Span> = {};
  for (const [name, box] of Object.entries(boxes)) {
    spans[name] = horizontal
      ? { alongLow: box.left, alongHigh: box.right, acrossLow: box.top, acrossHigh: box.bottom }
      : {
          alongLow: box.top,
          alongHigh: box.bottom,
          acrossLow: box.left,
          acrossHigh: box.right,
        };
  }
  const point = (along: number, across: number): Point =>
    horizontal ? { x: along, y: across } : { x: across, y: along };
  const names = Object.keys(spans);
  const inRank = (rank: number) => names.filter((name) => ranks.get(name) === rank);
  const rankExtent = (rank: number) => {
    const members = inRank(rank).map((name) => spans[name]);
    return {
      alongLow: Math.min(...members.map((span) => span.alongLow)),
      alongHigh: Math.max(...members.map((span) => span.alongHigh)),
    };
  };
  // A state on the near half of its rank keeps its back arcs on the near side.
  const sideOf = (name: string): 1 | -1 => {
    const members = inRank(ranks.get(name)!).map((other) => spans[other]);
    const middle =
      (Math.min(...members.map((span) => span.acrossLow)) +
        Math.max(...members.map((span) => span.acrossHigh))) /
      2;
    const span = spans[name];
    return (span.acrossLow + span.acrossHigh) / 2 < middle - 0.5 ? -1 : 1;
  };
  const middleAcross = (span: Span) => (span.acrossLow + span.acrossHigh) / 2;
  const middleAlong = (span: Span) => (span.alongLow + span.alongHigh) / 2;
  const edgeAcross = (span: Span, side: 1 | -1) =>
    side === 1 ? span.acrossHigh : span.acrossLow;
  // Another card of the same rank lies between this state and the lane on `side`.
  const blocked = (name: string, side: 1 | -1) =>
    inRank(ranks.get(name)!).some(
      (other) =>
        other !== name &&
        (side === 1
          ? spans[other].acrossLow >= spans[name].acrossHigh
          : spans[other].acrossHigh <= spans[name].acrossLow),
    );

  const routes: Record<string, MachineArcRoute> = {};
  const lanesTaken: { side: 1 | -1; low: number; high: number; lane: number }[] = [];
  const back = links
    .filter((link) => link.data.kind === "back")
    .sort(
      (left, right) =>
        Math.abs(ranks.get(left.source)! - ranks.get(left.target)!) -
        Math.abs(ranks.get(right.source)! - ranks.get(right.target)!),
    );
  for (const link of back) {
    const source = spans[link.source];
    const target = spans[link.target];
    const side = sideOf(link.source);
    const low = Math.min(ranks.get(link.source)!, ranks.get(link.target)!);
    const high = Math.max(ranks.get(link.source)!, ranks.get(link.target)!);
    const lane = lanesTaken.filter(
      (taken) => taken.side === side && taken.low <= high && low <= taken.high,
    ).length;
    lanesTaken.push({ side, low, high, lane });
    const spanned = names.filter((name) => {
      const rank = ranks.get(name)!;
      return rank >= low && rank <= high;
    });
    const outer =
      side === 1
        ? Math.max(...spanned.map((name) => spans[name].acrossHigh))
        : Math.min(...spanned.map((name) => spans[name].acrossLow));
    const laneAcross = outer + side * (LANE_CLEARANCE + lane * LANE_SPACING);
    // Attach a little apart from the centre so two arcs between a pair never share an end.
    const shift = (link.data.lane - ((link.data.lanes ?? 1) - 1) / 2) * PORT_SPREAD;
    const sourceAlong = middleAlong(source) + PORT_SPREAD + shift;
    const targetAlong = middleAlong(target) - PORT_SPREAD + shift;
    const sourceGap = rankExtent(ranks.get(link.source)!).alongHigh + DETOUR_GAP;
    const targetGap = rankExtent(ranks.get(link.target)!).alongLow - DETOUR_GAP;
    const sourceBlocked = blocked(link.source, side);
    const targetBlocked = blocked(link.target, side);
    const laneStart = sourceBlocked ? sourceGap : sourceAlong;
    const laneEnd = targetBlocked ? targetGap : targetAlong;
    const points: Point[] = [point(sourceAlong, edgeAcross(source, side))];
    if (sourceBlocked) {
      const out = edgeAcross(source, side) + side * DETOUR_STEP;
      points.push(point(sourceAlong, out), point(sourceGap, out));
    }
    points.push(point(laneStart, laneAcross), point(laneEnd, laneAcross));
    if (targetBlocked) {
      const out = edgeAcross(target, side) + side * DETOUR_STEP;
      points.push(point(targetGap, out), point(targetAlong, out));
    }
    points.push(point(targetAlong, edgeAcross(target, side)));
    // The label sits on the lane, halfway along it.
    const labelAlong = (laneStart + laneEnd) / 2;
    routes[link.id] = {
      path: roundedPath(points),
      label: point(labelAlong, laneAcross + (horizontal ? 0 : side * 8)),
      anchor: horizontal ? undefined : side === 1 ? "start" : "end",
    };
  }

  // Where each forward arc's label sits across the gap, relative to the pair's midline:
  // labels of one pair stack in lane order, each a label's extent plus a gap apart.
  const fanOffsets = new Map<string, number>();
  const pairs = new Map<string, MachineLink[]>();
  for (const link of links) {
    if (link.data.kind !== "forward") continue;
    const pair = JSON.stringify([link.source, link.target]);
    pairs.set(pair, [...(pairs.get(pair) ?? []), link]);
  }
  for (const group of pairs.values()) {
    group.sort((left, right) => left.data.lane - right.data.lane);
    const extents = group.map((link) => {
      const size = labelSize(link.data);
      return horizontal ? size.height : size.width;
    });
    const offsets = [0];
    for (let index = 1; index < group.length; index++)
      offsets.push(
        offsets[index - 1] + (extents[index - 1] + extents[index]) / 2 + LABEL_GAP[orientation],
      );
    const centre = (offsets[0] + offsets.at(-1)!) / 2;
    group.forEach((link, index) => fanOffsets.set(link.id, offsets[index] - centre));
  }

  for (const link of links) {
    const source = spans[link.source];
    const target = spans[link.target];
    if (link.data.kind === "self") {
      // A loop off the card's free side, away from its back arcs, nested per trigger.
      const side = -sideOf(link.source) as 1 | -1;
      const edge = edgeAcross(source, side);
      const reach = SELF_LOOP_REACH + link.data.lane * SELF_LOOP_STEP;
      const half = (source.alongHigh - source.alongLow) / 2;
      const leave = middleAlong(source) + half * 0.6 + link.data.lane * 14;
      const enter = middleAlong(source) + half * 0.25 - link.data.lane * 14;
      const loop = edge + side * reach;
      routes[link.id] = {
        path: roundedPath([
          point(leave, edge),
          point(leave, loop),
          point(enter, loop),
          point(enter, edge),
        ]),
        label: horizontal
          ? point((leave + enter) / 2, loop)
          : point((leave + enter) / 2, loop + side * 8),
        anchor: horizontal ? undefined : side === 1 ? "start" : "end",
      };
    } else if (link.data.kind === "forward") {
      // Forward arcs only join adjacent ranks, so they stay in the empty gap between.
      // Parallel arcs (one per trigger) fan out: each runs level through its own label,
      // and the labels stack across the gap far enough apart that no label touches
      // another label or a neighbouring arc.
      const position = link.data.lane - ((link.data.lanes ?? 1) - 1) / 2;
      const from = {
        along: source.alongHigh,
        across: middleAcross(source) + position * PORT_SPREAD,
      };
      const to = {
        along: target.alongLow,
        across: middleAcross(target) + position * PORT_SPREAD,
      };
      const middle = (from.along + to.along) / 2;
      const labelAcross =
        (middleAcross(source) + middleAcross(target)) / 2 + (fanOffsets.get(link.id) ?? 0);
      const half = Math.max(40, (to.along - from.along) / 2);
      // Level for most of each half, so the arc stays flat under its whole label.
      const pull = half * 0.25;
      const level = half * 0.8;
      const at = (along: number, across: number) => {
        const { x, y } = point(along, across);
        return `${Math.round(x * 10) / 10},${Math.round(y * 10) / 10}`;
      };
      routes[link.id] = {
        path:
          `M ${at(from.along, from.across)}` +
          ` C ${at(from.along + pull, from.across)} ${at(middle - level, labelAcross)} ${at(middle, labelAcross)}` +
          ` C ${at(middle + level, labelAcross)} ${at(to.along - pull, to.across)} ${at(to.along, to.across)}`,
        label: point(middle, labelAcross),
      };
    }
  }
  return routes;
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
