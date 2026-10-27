import { describe, expect, it } from "vitest";
import { arrangeGraph, createGraphLayout, placeInOrder, type GraphLayout } from "./graphLayout";
import type { GraphOrientation } from "./useGraphOrientation";
import type { WorkflowTopologyMsg } from "./model";

const topology = {
  nodeIds: ["source", "left", "right", "sink"],
  graph: {
    source: { children: ["left", "right", "sink"] },
    left: { children: ["sink"] },
    right: { children: ["sink"] },
  },
};

// A memo feeds two competing decks, a judge compares them, and the publisher reads all.
const bakeoff = {
  nodeIds: [
    "pick_topic",
    "write_memo",
    "build_minimal_deck",
    "build_dense_deck",
    "judge_decks",
    "publish_bakeoff",
  ],
  graph: {
    pick_topic: { children: ["write_memo"] },
    write_memo: {
      children: ["build_minimal_deck", "build_dense_deck", "judge_decks", "publish_bakeoff"],
    },
    build_minimal_deck: { children: ["judge_decks", "publish_bakeoff"] },
    build_dense_deck: { children: ["judge_decks", "publish_bakeoff"] },
    judge_decks: { children: ["publish_bakeoff"] },
  },
};

const DEFAULT_SIZE = { width: 360, height: 130 };

function rows(layout: GraphLayout): string[][] {
  return layout.layers.map((layer) =>
    layer.flatMap(({ nodeId }) => (nodeId === undefined ? [] : [nodeId])),
  );
}

interface Point {
  x: number;
  y: number;
}

function pathPoints(path: string): Point[] {
  return path
    .replace(/^M /, "")
    .split(" L ")
    .map((pair) => {
      const [x, y] = pair.split(",").map(Number);
      return { x, y };
    });
}

function segments(points: readonly Point[]): [Point, Point][] {
  return points.slice(1).map((point, index) => [points[index], point]);
}

function cardBox(
  position: Point,
  size: { width: number; height: number } = DEFAULT_SIZE,
): { left: number; right: number; top: number; bottom: number } {
  // Card origins are the left edge and vertical center.
  return {
    left: position.x,
    right: position.x + size.width,
    top: position.y - size.height / 2,
    bottom: position.y + size.height / 2,
  };
}

function overlapLength(first: [Point, Point], second: [Point, Point]): number {
  const horizontal = (segment: [Point, Point]) => segment[0].y === segment[1].y;
  if (horizontal(first) !== horizontal(second)) return 0;
  const axis = horizontal(first) ? "x" : "y";
  const line = horizontal(first) ? "y" : "x";
  if (first[0][line] !== second[0][line]) return 0;
  const span = (segment: [Point, Point]) => [
    Math.min(segment[0][axis], segment[1][axis]),
    Math.max(segment[0][axis], segment[1][axis]),
  ];
  const [firstLow, firstHigh] = span(first);
  const [secondLow, secondHigh] = span(second);
  return Math.min(firstHigh, secondHigh) - Math.max(firstLow, secondLow);
}

function crosses(first: [Point, Point], second: [Point, Point]): boolean {
  const horizontal = first[0].y === first[1].y ? first : second;
  const upright = horizontal === first ? second : first;
  if (upright[0].x !== upright[1].x || horizontal[0].y !== horizontal[1].y) return false;
  const x = upright[0].x;
  const y = horizontal[0].y;
  const between = (value: number, a: number, b: number) =>
    value > Math.min(a, b) && value < Math.max(a, b);
  return between(x, horizontal[0].x, horizontal[1].x) && between(y, upright[0].y, upright[1].y);
}

function arrange(
  orientation: GraphOrientation,
  graph: Pick<WorkflowTopologyMsg, "nodeIds" | "graph"> = bakeoff,
) {
  const layout = createGraphLayout(graph, orientation);
  return { layout, ...arrangeGraph(layout, new Map(), orientation) };
}

describe("workflow orientation layout", () => {
  it.each(["horizontal", "vertical"] as const)(
    "places an independent input beside preparation for its first consumer in %s layout",
    (orientation) => {
      const workflow = {
        nodeIds: ["collect", "instructions", "normalize", "propose"],
        graph: {
          collect: { children: ["normalize"] },
          instructions: { children: ["propose"] },
          normalize: { children: ["propose"] },
        },
      };
      const original = structuredClone(workflow);
      const layout = createGraphLayout(workflow, orientation);
      expect(rows(layout)).toEqual([["collect"], ["normalize", "instructions"], ["propose"]]);
      const { positions } = arrangeGraph(layout, new Map(), orientation);
      const forward = orientation === "vertical" ? "y" : "x";
      const across = orientation === "vertical" ? "x" : "y";
      expect(positions.instructions[forward]).toBe(positions.normalize[forward]);
      expect(positions.instructions[across]).toBeGreaterThan(positions.normalize[across]);
      expect(layout.edges.map(({ source, target }) => [source, target])).toEqual([
        ["collect", "normalize"],
        ["instructions", "propose"],
        ["normalize", "propose"],
      ]);
      expect(workflow).toEqual(original);
    },
  );

  it("keeps shared inputs before their earliest consumer and leaves unused roots at the start", () => {
    const layout = createGraphLayout(
      {
        nodeIds: ["start", "early_input", "later_input", "unused", "prepare", "first", "last"],
        graph: {
          start: { children: ["prepare"] },
          prepare: { children: ["first"] },
          first: { children: ["last"] },
          early_input: { children: ["last", "prepare"] },
          later_input: { children: ["last", "first"] },
        },
      },
      "vertical",
    );
    const [first, second, third, fourth] = rows(layout);
    expect(new Set(first)).toEqual(new Set(["start", "early_input", "unused"]));
    expect(new Set(second)).toEqual(new Set(["prepare", "later_input"]));
    expect(third).toEqual(["first"]);
    expect(fourth).toEqual(["last"]);
    const { positions } = arrangeGraph(layout, new Map(), "vertical");
    for (const edge of layout.edges) {
      expect(positions[edge.source].y).toBeLessThan(positions[edge.target].y);
    }
  });

  it.each(["horizontal", "vertical"] as const)(
    "preserves branches, joins, and skip dependencies in %s layout",
    (orientation) => {
      const { layout, positions } = arrange(orientation, topology);
      expect(layout.edges).toHaveLength(5);
      const forward = orientation === "vertical" ? "y" : "x";
      const across = orientation === "vertical" ? "x" : "y";
      for (const edge of layout.edges) {
        expect(positions[edge.target][forward]).toBeGreaterThan(
          positions[edge.source][forward],
        );
        expect([edge.sourceHandle, edge.targetHandle]).toEqual(
          orientation === "vertical"
            ? ["source-bottom", "target-top"]
            : ["source-right", "target-left"],
        );
      }
      expect(positions.left[forward]).toBe(positions.right[forward]);
      expect(positions.left[across]).not.toBe(positions.right[across]);
      // The skip connection reserves a lane between the branches it passes.
      const skip = layout.chains["source->sink"];
      expect(skip).toHaveLength(3);
      expect(layout.layers[1]).toContain(skip[1]);
    },
  );

  it("keeps duplicate dependencies as one connection", () => {
    const { layout } = arrange("horizontal", {
      nodeIds: ["a", "b"],
      graph: { a: { children: ["b", "b"] } },
    });
    expect(layout.edges.map((edge) => edge.id)).toEqual(["a->b"]);
  });

  it("keeps tall measured cards and parallel branches apart vertically", () => {
    const layout = createGraphLayout(topology, "vertical");
    const measurements = new Map([
      ["source", { width: 360, height: 400 }],
      ["left", { width: 360, height: 600 }],
      ["right", { width: 420, height: 130 }],
      ["sink", { width: 360, height: 250 }],
    ]);
    const { positions } = arrangeGraph(layout, measurements, "vertical");
    expect(positions.left.y - 300).toBeGreaterThan(positions.source.y + 200);
    expect(positions.sink.y - 125).toBeGreaterThan(positions.left.y + 300);
    expect(positions.right.x).toBeGreaterThan(positions.left.x + 360);
  });
});

describe.each(["horizontal", "vertical"] as const)("%s edge routing", (orientation) => {
  const { layout, positions, paths } = arrange(orientation);
  const routes = layout.edges.map((edge) => ({ edge, points: pathPoints(paths[edge.id]) }));

  it("leaves each source and lands each arrowhead on its own spot of the target card", () => {
    for (const { edge, points } of routes) {
      const source = cardBox(positions[edge.source]);
      const target = cardBox(positions[edge.target]);
      const start = points[0];
      const end = points.at(-1)!;
      const beforeEnd = points.at(-2)!;
      if (orientation === "horizontal") {
        expect(start.x).toBe(source.right);
        expect(start.y).toBeGreaterThan(source.top);
        expect(start.y).toBeLessThan(source.bottom);
        expect(end.x).toBe(target.left);
        expect(end.y).toBeGreaterThan(target.top);
        expect(end.y).toBeLessThan(target.bottom);
        // The final run points straight into the card.
        expect(beforeEnd.y).toBe(end.y);
      } else {
        expect(start.y).toBe(source.bottom);
        expect(start.x).toBeGreaterThan(source.left);
        expect(start.x).toBeLessThan(source.right);
        expect(end.y).toBe(target.top);
        expect(end.x).toBeGreaterThan(target.left);
        expect(end.x).toBeLessThan(target.right);
        expect(beforeEnd.x).toBe(end.x);
      }
    }
    const arrivals = routes.map(
      ({ edge, points }) => `${edge.target}@${JSON.stringify(points.at(-1))}`,
    );
    expect(new Set(arrivals).size).toBe(routes.length);
    const departures = routes.map(
      ({ edge, points }) => `${edge.source}@${JSON.stringify(points[0])}`,
    );
    expect(new Set(departures).size).toBe(routes.length);
  });

  it("never shares a segment between two connections", () => {
    for (const [index, first] of routes.entries()) {
      for (const second of routes.slice(index + 1)) {
        for (const a of segments(first.points)) {
          for (const b of segments(second.points)) {
            expect(overlapLength(a, b), `${first.edge.id} and ${second.edge.id}`).toBeLessThan(
              0.5,
            );
          }
        }
      }
    }
  });

  it("routes long connections between cards instead of through them", () => {
    for (const { edge, points } of routes) {
      for (const [nodeId, position] of Object.entries(positions)) {
        const box = cardBox(position);
        for (const [a, b] of segments(points)) {
          const left = Math.min(a.x, b.x);
          const right = Math.max(a.x, b.x);
          const top = Math.min(a.y, b.y);
          const bottom = Math.max(a.y, b.y);
          const inside =
            left < box.right && right > box.left && top < box.bottom && bottom > box.top;
          expect(inside, `${edge.id} crosses ${nodeId}`).toBe(false);
        }
      }
    }
  });

  it("always moves forward and only turns at right angles", () => {
    const forward = orientation === "vertical" ? "y" : "x";
    for (const { points } of routes) {
      for (const [a, b] of segments(points)) {
        expect(a.x === b.x || a.y === b.y).toBe(true);
        expect(b[forward]).toBeGreaterThanOrEqual(a[forward]);
      }
    }
  });

  it("avoids crossings and keeps aligned connections straight", () => {
    let crossings = 0;
    for (const [index, first] of routes.entries()) {
      for (const second of routes.slice(index + 1)) {
        for (const a of segments(first.points)) {
          for (const b of segments(second.points)) if (crosses(a, b)) crossings += 1;
        }
      }
    }
    expect(crossings).toBe(0);
    const straight = (edgeId: string) =>
      routes.find(({ edge }) => edge.id === edgeId)!.points.length === 2;
    expect(straight("pick_topic->write_memo")).toBe(true);
    expect(straight("judge_decks->publish_bakeoff")).toBe(true);
    // The memo's skip to the publisher runs level through both layers it passes.
    const skip = routes.find(({ edge }) => edge.id === "write_memo->publish_bakeoff")!.points;
    expect(skip.length).toBeLessThanOrEqual(6);
  });
});

describe("placeInOrder", () => {
  it("keeps the requested gaps while staying as close as possible to each target", () => {
    expect(placeInOrder([0, 0, 100], [10, 10])).toEqual([-5, 5, 100]);
    expect(placeInOrder([0, 0], [10], [3, 1])).toEqual([-2.5, 7.5]);
  });

  it("stays within bounds without breaking the gaps", () => {
    expect(placeInOrder([0, 50, 100], [20, 20], undefined, { low: 10, high: 60 })).toEqual([
      10, 40, 60,
    ]);
  });
});
