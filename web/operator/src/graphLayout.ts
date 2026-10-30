import { MarkerType, type Edge } from "@xyflow/react";
import type { WorkflowTopologyMsg } from "./model";
import type { GraphOrientation } from "./useGraphOrientation";

// One slot in a layer: a workflow node, or the point where a longer edge crosses
// that layer. Edges that skip layers reserve a slot in each skipped layer, so they
// pass between cards instead of wrapping around the outside of the graph. Long edges
// from one source, or into one target, share those slots and run as one trunk.
export interface LayerItem {
  nodeId: string | undefined;
}

export interface RoutedEdgeData extends Record<string, unknown> {
  path: string;
}

export interface GraphLayout {
  edges: Edge[];
  layers: LayerItem[][];
  // Layer items each edge passes through, from its source node to its target node.
  chains: Record<string, LayerItem[]>;
}

interface Size {
  width: number;
  height: number;
}

export interface GraphArrangement {
  positions: Record<string, { x: number; y: number }>;
  paths: Record<string, string>;
}

const DEFAULT_SIZE: Size = { width: 360, height: 130 };
const NODE_GAP = { horizontal: 100, vertical: 60 };
const LAYER_GAP = { horizontal: 140, vertical: 100 };
const WAYPOINT_GAP = 40;
const WAYPOINT_SPACING = 20;
const PORT_INSET = 10;
const PORT_SPACING = 16;
const CHANNEL_MARGIN = 28;
const CHANNEL_SPACING = 16;
// Long edges pull much harder on each other than on cards, so they run straight, and
// cards follow the cards they connect to rather than edges passing by.
const WAYPOINT_WEIGHT = 8;
const CARD_WEIGHT = 2;
const ORDERING_SWEEPS = 8;
const PLACEMENT_SWEEPS = 12;

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
  const nodeLayers: LayerItem[][] = [];
  const items = new Map<string, LayerItem>();
  // Keep the existing preparation steps first and place supporting inputs beside them.
  for (const nodeId of [
    ...topology.nodeIds.filter((id) => !deferredInputs.has(id)),
    ...deferredInputs,
  ]) {
    const item = { nodeId };
    items.set(nodeId, item);
    (nodeLayers[depths[nodeId] ?? 0] ??= []).push(item);
  }
  for (let depth = 0; depth < nodeLayers.length; depth += 1) nodeLayers[depth] ??= [];

  const edges: Edge[] = [];
  const links = new Set<string>();
  for (const [source, children] of Object.entries(topology.graph)) {
    for (const target of children.children) {
      const id = `${source}->${target}`;
      if (links.has(id)) continue;
      links.add(id);
      edges.push({
        id,
        source,
        target,
        sourceHandle: orientation === "vertical" ? "source-bottom" : "source-right",
        targetHandle: orientation === "vertical" ? "target-top" : "target-left",
        markerEnd: { type: MarkerType.ArrowClosed },
        type: "routed",
        className: "dag-edge",
      });
    }
  }
  // Shared trunks usually remove parallel lanes, but they can force an edge across
  // another one; keep them only when they add no crossings.
  const separate = routeEdges(edges, nodeLayers, items, depths, false);
  const shared = routeEdges(edges, nodeLayers, items, depths, true);
  const { layers, chains } = shared.crossings <= separate.crossings ? shared : separate;
  return { edges, layers, chains };
}

function routeEdges(
  edges: readonly Edge[],
  nodeLayers: readonly LayerItem[][],
  items: ReadonlyMap<string, LayerItem>,
  depths: Readonly<Record<string, number>>,
  shareTrunks: boolean,
) {
  // A source with several long edges sends them along one trunk that branches off
  // toward each target. Other long edges into one target merge into one trunk when
  // their sources sit at different depths; sibling sources keep their own lanes so
  // each can pass on its own side of the cards between them.
  const long = edges.filter(({ source, target }) => depths[target] > depths[source] + 1);
  const trunks = new Set(
    long
      .map(({ source }) => source)
      .filter((source, index, sources) => shareTrunks && sources.indexOf(source) !== index),
  );
  const mergingDepths = new Map<string, number[]>();
  for (const { source, target } of long) {
    if (!shareTrunks || trunks.has(source)) continue;
    mergingDepths.set(target, [...(mergingDepths.get(target) ?? []), depths[source]]);
  }
  const layers = nodeLayers.map((layer) => [...layer]);
  const waypoints = new Map<string, LayerItem>();
  const chains: Record<string, LayerItem[]> = {};
  for (const { id, source, target } of edges) {
    const lane = trunks.has(source)
      ? `from:${source}`
      : mergingDepths.get(target)?.filter((depth) => depth === depths[source]).length === 1
        ? `into:${target}`
        : id;
    const chain = [items.get(source)!];
    for (let depth = depths[source] + 1; depth < depths[target]; depth += 1) {
      const key = `${lane}@${depth}`;
      let waypoint = waypoints.get(key);
      if (waypoint === undefined) {
        waypoint = { nodeId: undefined };
        waypoints.set(key, waypoint);
        layers[depth].push(waypoint);
      }
      chain.push(waypoint);
    }
    chain.push(items.get(target)!);
    chains[id] = chain;
  }
  const crossings = orderLayers(layers, Object.values(chains));
  return { layers, chains, crossings };
}

function neighborMaps(chains: readonly LayerItem[][]) {
  const above = new Map<LayerItem, LayerItem[]>();
  const below = new Map<LayerItem, LayerItem[]>();
  for (const chain of chains) {
    for (let index = 1; index < chain.length; index += 1) {
      const upper = chain[index - 1];
      const lower = chain[index];
      if (!above.has(lower)) above.set(lower, []);
      if (!below.has(upper)) below.set(upper, []);
      // Edges sharing a trunk share its links; count each link once.
      if (above.get(lower)!.includes(upper)) continue;
      above.get(lower)!.push(upper);
      below.get(upper)!.push(lower);
    }
  }
  return { above, below };
}

function countCrossings(
  layers: readonly LayerItem[][],
  below: ReadonlyMap<LayerItem, LayerItem[]>,
): number {
  let crossings = 0;
  for (let depth = 0; depth + 1 < layers.length; depth += 1) {
    const lowerIndex = new Map(layers[depth + 1].map((item, index) => [item, index]));
    const links = layers[depth].flatMap((item, upper) =>
      (below.get(item) ?? []).map((lower) => [upper, lowerIndex.get(lower)!] as const),
    );
    for (let first = 0; first < links.length; first += 1) {
      for (let second = first + 1; second < links.length; second += 1) {
        const [upperA, lowerA] = links[first];
        const [upperB, lowerB] = links[second];
        if ((upperA - upperB) * (lowerA - lowerB) < 0) crossings += 1;
      }
    }
  }
  return crossings;
}

// Barycenter sweeps: each item moves toward the average slot of its neighbors in
// the adjacent layer. The ordering with the fewest crossings wins.
function orderLayers(layers: LayerItem[][], chains: readonly LayerItem[][]): number {
  const { above, below } = neighborMaps(chains);
  const sortLayer = (depth: number, neighbors: ReadonlyMap<LayerItem, LayerItem[]>) => {
    const reference = layers[depth + (neighbors === above ? -1 : 1)];
    const slots = new Map(reference.map((item, index) => [item, index]));
    const keyed = layers[depth].map((item, index) => {
      const adjacent = neighbors.get(item) ?? [];
      const key =
        adjacent.length === 0
          ? index
          : adjacent.reduce((sum, other) => sum + slots.get(other)!, 0) / adjacent.length;
      return { item, key };
    });
    keyed.sort((left, right) => left.key - right.key);
    layers[depth] = keyed.map(({ item }) => item);
  };
  for (let depth = 1; depth < layers.length; depth += 1) sortLayer(depth, above);
  let best = layers.map((layer) => [...layer]);
  let fewest = countCrossings(layers, below);
  for (let sweep = 0; sweep < ORDERING_SWEEPS && fewest > 0; sweep += 1) {
    if (sweep % 2 === 0) {
      for (let depth = layers.length - 2; depth >= 0; depth -= 1) sortLayer(depth, below);
    } else {
      for (let depth = 1; depth < layers.length; depth += 1) sortLayer(depth, above);
    }
    const crossings = countCrossings(layers, below);
    if (crossings < fewest) {
      fewest = crossings;
      best = layers.map((layer) => [...layer]);
    }
  }
  best.forEach((layer, depth) => (layers[depth] = layer));
  return fewest;
}

// Closest positions to `desired` that keep each neighbor at least `gaps[i]` after
// the previous one and, when given, everything within `bounds`: weighted isotonic
// regression (pool adjacent violators) on values shifted by the required gaps.
export function placeInOrder(
  desired: readonly number[],
  gaps: readonly number[],
  weights: readonly number[] = desired.map(() => 1),
  bounds?: { low: number; high: number },
): number[] {
  const offsets = [0];
  for (const gap of gaps) offsets.push(offsets.at(-1)! + gap);
  const blocks: { sum: number; weight: number; count: number }[] = [];
  desired.forEach((value, index) => {
    blocks.push({
      sum: weights[index] * (value - offsets[index]),
      weight: weights[index],
      count: 1,
    });
    while (blocks.length > 1) {
      const last = blocks[blocks.length - 1];
      const previous = blocks[blocks.length - 2];
      if (previous.sum / previous.weight <= last.sum / last.weight) break;
      previous.sum += last.sum;
      previous.weight += last.weight;
      previous.count += last.count;
      blocks.pop();
    }
  });
  const low = bounds?.low ?? -Infinity;
  const high = (bounds?.high ?? Infinity) - offsets.at(-1)!;
  return blocks
    .flatMap(({ sum, weight, count }) => Array<number>(count).fill(sum / weight))
    .map((value, index) => Math.min(high, Math.max(low, value)) + offsets[index]);
}

export interface Pull {
  value: number;
  weight: number;
}

// Positions that keep each neighbor at least `gaps[i]` after the previous one and
// minimize the weighted distance from every item to each of its pulls. Unlike
// averaging, the weighted median lets a card sit exactly level with the neighbor
// that pulls hardest, so a chain stays straight while other edges bend to reach it.
export function placeAtMedians(
  pulls: readonly (readonly Pull[])[],
  gaps: readonly number[],
): number[] {
  const offsets = [0];
  for (const gap of gaps) offsets.push(offsets.at(-1)! + gap);
  const blocks: { pulls: Pull[]; value: number; count: number }[] = [];
  pulls.forEach((itemPulls, index) => {
    const shifted = itemPulls.map(({ value, weight }) => ({
      value: value - offsets[index],
      weight,
    }));
    blocks.push({ pulls: shifted, value: weightedMedian(shifted), count: 1 });
    while (blocks.length > 1) {
      const last = blocks[blocks.length - 1];
      const previous = blocks[blocks.length - 2];
      if (previous.value <= last.value) break;
      previous.pulls.push(...last.pulls);
      previous.value = weightedMedian(previous.pulls);
      previous.count += last.count;
      blocks.pop();
    }
  });
  return blocks
    .flatMap(({ value, count }) => Array<number>(count).fill(value))
    .map((value, index) => value + offsets[index]);
}

// The middle of the range of values that minimize the total weighted distance.
function weightedMedian(pulls: readonly Pull[]): number {
  const sorted = [...pulls].sort((left, right) => left.value - right.value);
  const half = sorted.reduce((sum, { weight }) => sum + weight, 0) / 2;
  let below = 0;
  for (const [index, { value, weight }] of sorted.entries()) {
    below += weight;
    if (below > half + 1e-9) return value;
    if (below > half - 1e-9) return (value + sorted[index + 1].value) / 2;
  }
  throw new Error("Cannot place an item without pulls");
}

// Spread ports along one side of a card, as close to their other ends as the card allows.
function placePorts(desired: readonly number[], low: number, high: number): number[] {
  if (desired.length === 0) return [];
  if (desired.length > 1 && (desired.length - 1) * PORT_SPACING > high - low) {
    const step = (high - low) / (desired.length - 1);
    return desired.map((_, index) => low + index * step);
  }
  return placeInOrder(
    desired,
    desired.slice(1).map(() => PORT_SPACING),
    undefined,
    { low, high },
  );
}

interface Segment {
  // One hop between two layer items; edges sharing a trunk share its hops.
  hop: string;
  from: number;
  to: number;
}

// Cost of drawing `left`'s vertical before `right`'s within one gap between layers.
function orderingCost(left: Segment, right: Segment): number {
  const inside = (value: number, segment: Segment) =>
    value > Math.min(segment.from, segment.to) && value < Math.max(segment.from, segment.to);
  // Two horizontal runs on the same line would overlap between the two verticals.
  if (Math.abs(left.to - right.from) < 0.5) return 100;
  return Number(inside(left.to, right)) + Number(inside(right.from, left));
}

function orderChannels(segments: readonly Segment[]): Segment[] {
  const ordered: Segment[] = [];
  const bySpan = [...segments].sort(
    (left, right) => Math.abs(right.to - right.from) - Math.abs(left.to - left.from),
  );
  for (const segment of bySpan) {
    let cost = ordered.reduce((sum, other) => sum + orderingCost(segment, other), 0);
    let bestCost = cost;
    let bestIndex = 0;
    ordered.forEach((other, index) => {
      cost += orderingCost(other, segment) - orderingCost(segment, other);
      if (cost < bestCost) {
        bestCost = cost;
        bestIndex = index + 1;
      }
    });
    ordered.splice(bestIndex, 0, segment);
  }
  return ordered;
}

export function arrangeGraph(
  layout: GraphLayout,
  measurements: ReadonlyMap<string, Size>,
  orientation: GraphOrientation,
): GraphArrangement {
  const vertical = orientation === "vertical";
  const size = (nodeId: string) => measurements.get(nodeId) ?? DEFAULT_SIZE;
  const acrossSize = (item: LayerItem) =>
    item.nodeId === undefined
      ? 0
      : vertical
        ? size(item.nodeId).width
        : size(item.nodeId).height;
  const forwardSize = (nodeId: string) => (vertical ? size(nodeId).height : size(nodeId).width);
  const gapBetween = (first: LayerItem, second: LayerItem) => {
    const half = (acrossSize(first) + acrossSize(second)) / 2;
    if (first.nodeId !== undefined && second.nodeId !== undefined) {
      return half + NODE_GAP[orientation];
    }
    return (
      half +
      (first.nodeId === undefined && second.nodeId === undefined
        ? WAYPOINT_SPACING
        : WAYPOINT_GAP)
    );
  };

  // Across the layers: start packed and centered, then pull every item toward its
  // neighbors in alternating sweeps so connected cards line up and edges stay short.
  const { above, below } = neighborMaps(Object.values(layout.chains));
  const across = new Map<LayerItem, number>();
  const gaps = layout.layers.map((layer) =>
    layer.slice(1).map((item, index) => gapBetween(layer[index], item)),
  );
  layout.layers.forEach((layer, depth) => {
    const packed = placeInOrder(
      layer.map(() => 0),
      gaps[depth],
    );
    const middle = ((packed[0] ?? 0) + (packed.at(-1) ?? 0)) / 2;
    layer.forEach((item, index) => across.set(item, packed[index] - middle));
  });
  const linkWeight = (item: LayerItem, other: LayerItem) =>
    item.nodeId === undefined && other.nodeId === undefined
      ? WAYPOINT_WEIGHT
      : item.nodeId !== undefined && other.nodeId !== undefined
        ? CARD_WEIGHT
        : 1;
  const align = (
    depth: number,
    neighborSets: readonly ReadonlyMap<LayerItem, LayerItem[]>[],
  ) => {
    const layer = layout.layers[depth];
    const pulls = layer.map((item) => {
      const adjacent = neighborSets.flatMap((neighbors) => neighbors.get(item) ?? []);
      if (adjacent.length === 0) return [{ value: across.get(item)!, weight: 1 }];
      return adjacent.map((other) => ({
        value: across.get(other)!,
        weight: linkWeight(item, other),
      }));
    });
    const placed = placeAtMedians(pulls, gaps[depth]);
    layer.forEach((item, index) => across.set(item, placed[index]));
  };
  for (let sweep = 0; sweep < PLACEMENT_SWEEPS; sweep += 1) {
    // Early sweeps settle each layer against one side; later ones balance both.
    const settled = sweep >= PLACEMENT_SWEEPS / 2;
    for (let depth = 1; depth < layout.layers.length; depth += 1) {
      align(depth, settled ? [above, below] : [above]);
    }
    for (let depth = layout.layers.length - 2; depth >= 0; depth -= 1) {
      align(depth, settled ? [above, below] : [below]);
    }
    // Ties between medians let the whole graph slide without changing its shape;
    // keep it centered so the view does not jump when card sizes are measured.
    const placed = [...across.values()];
    const middle = (Math.min(...placed) + Math.max(...placed)) / 2;
    for (const [item, value] of across) across.set(item, value - middle);
  }

  // Run each long edge straight through the layers it skips wherever its lane
  // allows, preferably level with a port on its source or target card.
  const portRange = (item: LayerItem) => {
    const center = across.get(item)!;
    const half = Math.max(0, acrossSize(item) / 2 - PORT_INSET);
    return [center - half, center + half] as const;
  };
  const slots = new Map<LayerItem, { depth: number; index: number }>();
  layout.layers.forEach((layer, depth) =>
    layer.forEach((item, index) => slots.set(item, { depth, index })),
  );
  const fits = (item: LayerItem, value: number) => {
    const { depth, index } = slots.get(item)!;
    const before = layout.layers[depth][index - 1];
    const after = layout.layers[depth][index + 1];
    return (
      (before === undefined || value >= across.get(before)! + gaps[depth][index - 1] - 0.01) &&
      (after === undefined || value <= across.get(after)! - gaps[depth][index] + 0.01)
    );
  };
  const clampInto = ([low, high]: readonly [number, number], value: number) =>
    Math.min(high, Math.max(low, value));
  // Waypoints linked to each other form one trunk, which runs at a single level.
  const trunkOf = new Map<LayerItem, LayerItem[]>();
  for (const chain of Object.values(layout.chains)) {
    let trunk: LayerItem[] | undefined;
    for (const item of chain.slice(1, -1)) {
      const existing = trunkOf.get(item);
      if (trunk === undefined) {
        trunk = existing ?? [item];
      } else if (existing !== undefined && existing !== trunk) {
        for (const other of existing) trunkOf.set(other, trunk);
        trunk.push(...existing);
      } else if (existing === undefined) {
        trunk.push(item);
      }
      trunkOf.set(item, trunk);
    }
  }
  const trunks = [...new Set(trunkOf.values())].sort(
    (left, right) => right.length - left.length,
  );
  for (const waypoints of trunks) {
    const current = waypoints.map((item) => across.get(item)!);
    const mean = current.reduce((sum, value) => sum + value, 0) / current.length;
    const ends = [
      ...new Set(
        waypoints.flatMap((item) =>
          [...(above.get(item) ?? []), ...(below.get(item) ?? [])].filter(
            (other) => other.nodeId !== undefined,
          ),
        ),
      ),
    ];
    const candidates = [
      ...ends.map((end) => clampInto(portRange(end), mean)),
      ...current.sort((left, right) => Math.abs(left - mean) - Math.abs(right - mean)),
    ];
    const level = candidates.find((value) => waypoints.every((item) => fits(item, value)));
    if (level !== undefined) for (const item of waypoints) across.set(item, level);
  }

  // Each link leaves and enters through its own port, so arrowheads never stack.
  // Edges sharing a trunk share the port where the trunk meets the card.
  const outPorts: Record<string, number> = {};
  const inPorts: Record<string, number> = {};
  const outgoing = new Map<LayerItem, Map<LayerItem, string[]>>();
  const incoming = new Map<LayerItem, Map<LayerItem, string[]>>();
  const link = (
    links: Map<LayerItem, Map<LayerItem, string[]>>,
    card: LayerItem,
    other: LayerItem,
    edgeId: string,
  ) => {
    if (!links.has(card)) links.set(card, new Map());
    const byOther = links.get(card)!;
    if (!byOther.has(other)) byOther.set(other, []);
    byOther.get(other)!.push(edgeId);
  };
  for (const [edgeId, chain] of Object.entries(layout.chains)) {
    link(outgoing, chain[0], chain[1], edgeId);
    link(incoming, chain.at(-1)!, chain.at(-2)!, edgeId);
  }
  for (const [item, links] of outgoing) {
    const ordered = [...links].sort(
      ([left], [right]) => across.get(left)! - across.get(right)!,
    );
    const ports = placePorts(
      ordered.map(([next]) => across.get(next)!),
      ...portRange(item),
    );
    ordered.forEach(([, edgeIds], index) => {
      for (const edgeId of edgeIds) outPorts[edgeId] = ports[index];
    });
  }
  const endAcross = (edgeId: string, item: LayerItem) =>
    item.nodeId === undefined ? across.get(item)! : outPorts[edgeId];
  for (const [item, links] of incoming) {
    const ordered = [...links].sort(
      ([leftItem, [left]], [rightItem, [right]]) =>
        endAcross(left, leftItem) - endAcross(right, rightItem),
    );
    const ports = placePorts(
      ordered.map(([previous, [edgeId]]) => endAcross(edgeId, previous)),
      ...portRange(item),
    );
    ordered.forEach(([, edgeIds], index) => {
      for (const edgeId of edgeIds) inPorts[edgeId] = ports[index];
    });
  }

  // Between two layers, every bending hop gets its own vertical channel.
  const depthOf = new Map<LayerItem, number>();
  const itemIndex = new Map<LayerItem, number>();
  layout.layers.forEach((layer, depth) =>
    layer.forEach((item) => {
      depthOf.set(item, depth);
      itemIndex.set(item, itemIndex.size);
    }),
  );
  const hopId = (chain: LayerItem[], index: number) =>
    `${itemIndex.get(chain[index])}>${itemIndex.get(chain[index + 1])}`;
  const hopAcross = (edgeId: string, chain: LayerItem[], index: number) => {
    const from = index === 0 ? outPorts[edgeId] : across.get(chain[index])!;
    const to = index + 2 === chain.length ? inPorts[edgeId] : across.get(chain[index + 1])!;
    return { from, to };
  };
  const gapSegments: Segment[][] = layout.layers.map(() => []);
  const seenHops = new Set<string>();
  for (const [edgeId, chain] of Object.entries(layout.chains)) {
    for (let index = 0; index + 1 < chain.length; index += 1) {
      const hop = hopId(chain, index);
      if (seenHops.has(hop)) continue;
      seenHops.add(hop);
      const { from, to } = hopAcross(edgeId, chain, index);
      if (Math.abs(from - to) >= 0.5) {
        gapSegments[depthOf.get(chain[index])!].push({ hop, from, to });
      }
    }
  }
  const channelOrder = gapSegments.map(orderChannels);

  // Along the layers: leave enough room in each gap for its channels.
  const layerSizes = layout.layers.map((layer) =>
    Math.max(0, ...layer.flatMap((item) => (item.nodeId ? [forwardSize(item.nodeId)] : []))),
  );
  const layerStarts: number[] = [];
  let forward = 0;
  layout.layers.forEach((_, depth) => {
    layerStarts.push(forward);
    const channels = channelOrder[depth].length;
    forward +=
      layerSizes[depth] +
      Math.max(LAYER_GAP[orientation], 2 * CHANNEL_MARGIN + (channels - 1) * CHANNEL_SPACING);
  });
  const channels: Record<string, number>[] = channelOrder.map((ordered, depth) => {
    const start = layerStarts[depth] + layerSizes[depth];
    const end = layerStarts[depth + 1] ?? start;
    const step = (end - start - 2 * CHANNEL_MARGIN) / Math.max(1, ordered.length - 1);
    const first = ordered.length === 1 ? (start + end) / 2 : start + CHANNEL_MARGIN;
    return Object.fromEntries(ordered.map(({ hop }, index) => [hop, first + index * step]));
  });

  const nodeStart = (nodeId: string, depth: number) =>
    layerStarts[depth] + (vertical ? (layerSizes[depth] - forwardSize(nodeId)) / 2 : 0);
  const positions: Record<string, { x: number; y: number }> = {};
  layout.layers.forEach((layer, depth) => {
    for (const { nodeId } of layer) {
      if (nodeId === undefined) continue;
      const center = across.get(layer.find((item) => item.nodeId === nodeId)!)!;
      const start = nodeStart(nodeId, depth);
      // Origins are at the left edge and vertical center of each card in both layouts.
      positions[nodeId] = vertical
        ? { x: center - size(nodeId).width / 2, y: start + size(nodeId).height / 2 }
        : { x: start, y: center };
    }
  });

  const point = (along: number, side: number) =>
    vertical ? `${round(side)},${round(along)}` : `${round(along)},${round(side)}`;
  const paths: Record<string, string> = {};
  for (const [edgeId, chain] of Object.entries(layout.chains)) {
    const source = chain[0];
    const target = chain.at(-1)!;
    const sourceDepth = depthOf.get(source)!;
    const points = [
      point(
        nodeStart(source.nodeId!, sourceDepth) + forwardSize(source.nodeId!),
        outPorts[edgeId],
      ),
    ];
    for (let index = 0; index + 1 < chain.length; index += 1) {
      const channel = channels[sourceDepth + index][hopId(chain, index)];
      if (channel === undefined) continue;
      const { from, to } = hopAcross(edgeId, chain, index);
      points.push(point(channel, from), point(channel, to));
    }
    points.push(point(nodeStart(target.nodeId!, depthOf.get(target)!), inPorts[edgeId]));
    paths[edgeId] = `M ${points.join(" L ")}`;
  }
  return { positions, paths };
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}
