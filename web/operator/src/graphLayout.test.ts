import { describe, expect, it } from "vitest";
import {
  arrangeGraph,
  createGraphLayout,
  assignColumns,
  placeAtMedians,
  placeInOrder,
  type GraphLayout,
} from "./graphLayout";
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

  it("leaves and enters each card through one spot per link or shared trunk", () => {
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
    // Edges share a spot exactly when they share the hop next to the card.
    const ids = new Map<object, number>();
    const id = (item: object) => ids.get(item) ?? ids.set(item, ids.size).get(item)!;
    const spots = (end: (points: Point[]) => Point, hop: (chain: object[]) => object[]) => {
      const hops = routes.map(({ edge }) => hop(layout.chains[edge.id]).map(id).join(">"));
      const points = routes.map(({ points }) => JSON.stringify(end(points)));
      return [new Set(points).size, new Set(hops).size];
    };
    const [arrivals, arrivalHops] = spots(
      (points) => points.at(-1)!,
      (chain) => chain.slice(-2),
    );
    expect(arrivals).toBe(arrivalHops);
    const [departures, departureHops] = spots(
      (points) => points[0],
      (chain) => chain.slice(0, 2),
    );
    expect(departures).toBe(departureHops);
  });

  it("shares segments only along a trunk the connections have in common", () => {
    for (const [index, first] of routes.entries()) {
      for (const second of routes.slice(index + 1)) {
        const trunk = layout.chains[first.edge.id]
          .slice(1, -1)
          .some((item) => layout.chains[second.edge.id].includes(item));
        if (trunk) continue;
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

describe("straight rows", () => {
  // The run input feeds every step of a chain, as in the operator demo workflow.
  const inputChain = {
    nodeIds: ["input", "fetch", "validate", "summarize", "publish"],
    graph: {
      input: { children: ["fetch", "validate", "summarize", "publish"] },
      fetch: { children: ["validate"] },
      validate: { children: ["summarize"] },
      summarize: { children: ["publish"] },
    },
  };

  it.each(["horizontal", "vertical"] as const)(
    "keeps a chain fed by one source level and sends its long edges along one trunk in %s layout",
    (orientation) => {
      const { layout, positions, paths } = arrange(orientation, inputChain);
      const across = orientation === "vertical" ? "x" : "y";
      const level = new Set(inputChain.nodeIds.map((nodeId) => positions[nodeId][across]));
      expect(level.size).toBe(1);
      const long = ["validate", "summarize", "publish"].map((target) =>
        pathPoints(paths[`input->${target}`]),
      );
      expect(new Set(long.map((points) => JSON.stringify(points[0]))).size).toBe(1);
      // Every long edge runs on the same line beside the row, apart from the row itself.
      const runs = new Set(long.map((points) => points[2][across]));
      expect(runs.size).toBe(1);
      expect([...runs][0]).not.toBe(positions.fetch[across]);
      const trunk = layout.chains["input->publish"].slice(1, -1);
      expect(layout.chains["input->summarize"].slice(1, -1)).toEqual(trunk.slice(0, 2));
      expect(layout.layers.map((layer) => layer.length)).toEqual([1, 2, 2, 2, 1]);
    },
  );

  it.each(["horizontal", "vertical"] as const)(
    "keeps separate lines in %s layout where a shared trunk would cross another edge",
    (orientation) => {
      // The memo's skips to the judge and the publisher would have to cross the
      // deck siblings' own skips if they shared one trunk.
      const { layout, paths } = arrange(orientation, bakeoff);
      const memoToJudge = layout.chains["write_memo->judge_decks"].slice(1, -1);
      const memoToPublish = layout.chains["write_memo->publish_bakeoff"].slice(1, -1);
      expect(memoToJudge.some((item) => memoToPublish.includes(item))).toBe(false);
      const minimal = layout.chains["build_minimal_deck->publish_bakeoff"].slice(1, -1);
      const dense = layout.chains["build_dense_deck->publish_bakeoff"].slice(1, -1);
      expect(minimal.some((item) => dense.includes(item))).toBe(false);
      const end = (edgeId: string) => JSON.stringify(pathPoints(paths[edgeId]).at(-1));
      expect(end("build_minimal_deck->publish_bakeoff")).not.toBe(
        end("build_dense_deck->publish_bakeoff"),
      );
    },
  );

  it("keeps a plain chain on one line with straight edges", () => {
    const chain = {
      nodeIds: ["load", "parse", "score", "report"],
      graph: {
        load: { children: ["parse"] },
        parse: { children: ["score"] },
        score: { children: ["report"] },
      },
    };
    const { positions, paths } = arrange("horizontal", chain);
    expect(new Set(chain.nodeIds.map((nodeId) => positions[nodeId].y)).size).toBe(1);
    for (const path of Object.values(paths)) expect(pathPoints(path)).toHaveLength(2);
  });

  it("merges long edges into one target and keeps the chain they skip level", () => {
    const skips = {
      nodeIds: ["load", "parse", "first", "second", "finalize"],
      graph: {
        load: { children: ["parse", "finalize"] },
        parse: { children: ["first", "finalize"] },
        first: { children: ["second"] },
        second: { children: ["finalize"] },
      },
    };
    const { layout, positions, paths } = arrange("horizontal", skips);
    expect(new Set(skips.nodeIds.map((nodeId) => positions[nodeId].y)).size).toBe(1);
    const end = (edgeId: string) => JSON.stringify(pathPoints(paths[edgeId]).at(-1));
    expect(end("load->finalize")).toBe(end("parse->finalize"));
    expect(end("second->finalize")).not.toBe(end("load->finalize"));
    expect(layout.chains["load->finalize"].slice(2, -1)).toEqual(
      layout.chains["parse->finalize"].slice(1, -1),
    );
  });

  it.each([
    ["a fan-out", { load: { children: ["a", "b", "c"] } }, "load", ["a", "b", "c"]],
    ["a fan-in", { a: { children: ["join"] }, b: { children: ["join"] } }, "join", ["a", "b"]],
    [
      "a diamond",
      { load: { children: ["a", "b"] }, a: { children: ["join"] }, b: { children: ["join"] } },
      "join",
      ["a", "b"],
    ],
  ])("centers the shared card of %s between its branches", (_, graph, center, branches) => {
    const nodeIds = [...new Set([...Object.keys(graph), ...branches, center])];
    const { positions } = arrange("horizontal", { nodeIds, graph });
    const ys = branches.map((nodeId) => positions[nodeId].y);
    expect(positions[center].y).toBeCloseTo((Math.min(...ys) + Math.max(...ys)) / 2);
    expect(new Set(ys).size).toBe(branches.length);
  });

  it.each(["horizontal", "vertical"] as const)(
    "centers the chains before a split and after its join on one line in %s layout",
    (orientation) => {
      const across = orientation === "vertical" ? "x" : "y";
      // The input also feeds the last step, which must not pull the join chain aside.
      const { positions } = arrange(orientation, {
        nodeIds: ["input", "plan", "research", "draft", "review", "send"],
        graph: {
          input: { children: ["plan", "draft", "send"] },
          plan: { children: ["research", "draft"] },
          research: { children: ["review"] },
          draft: { children: ["review"] },
          review: { children: ["send"] },
        },
      });
      const middle = (positions.research[across] + positions.draft[across]) / 2;
      for (const nodeId of ["input", "plan", "review", "send"]) {
        expect(positions[nodeId][across], nodeId).toBeCloseTo(middle);
      }
    },
  );

  it("keeps the graph centered however many sweeps its medians tie", () => {
    const { positions } = arrange("horizontal", {
      nodeIds: ["input", "plan", "research", "draft", "review", "send"],
      graph: {
        input: { children: ["plan", "draft", "send"] },
        plan: { children: ["research", "draft"] },
        research: { children: ["review"] },
        draft: { children: ["review"] },
        review: { children: ["send"] },
      },
    });
    const ys = Object.values(positions).map(({ y }) => y);
    expect(Math.abs(Math.min(...ys) + Math.max(...ys))).toBeLessThan(DEFAULT_SIZE.height);
  });
});

describe.each(["horizontal", "vertical"] as const)("%s symmetry", (orientation) => {
  const forward = orientation === "vertical" ? "y" : "x";
  // Where a route turns from leaving its source toward its target.
  const bend = (path: string) => pathPoints(path)[1][forward];

  it("bends mirrored branches of a fan-out and a fan-in at the same point", () => {
    const fanOut = arrange(orientation, {
      nodeIds: ["load", "a", "b", "c"],
      graph: { load: { children: ["a", "b", "c"] } },
    }).paths;
    expect(bend(fanOut["load->a"])).toBe(bend(fanOut["load->c"]));
    const fanIn = arrange(orientation, {
      nodeIds: ["a", "b", "c", "join"],
      graph: {
        a: { children: ["join"] },
        b: { children: ["join"] },
        c: { children: ["join"] },
      },
    }).paths;
    expect(bend(fanIn["a->join"])).toBe(bend(fanIn["c->join"]));
  });

  it("mirrors a wide split and its join", () => {
    const shards = ["s1", "s2", "s3", "s4", "s5", "s6"];
    const { positions, paths } = arrange(orientation, {
      nodeIds: ["load", ...shards, "gather"],
      graph: {
        load: { children: shards },
        ...Object.fromEntries(shards.map((shard) => [shard, { children: ["gather"] }])),
      },
    });
    const sourceEnd = pathPoints(paths["load->s1"])[0][forward];
    const targetStart = pathPoints(paths["s1->gather"]).at(-1)![forward];
    const shardStart =
      positions.s1[forward] - (orientation === "vertical" ? DEFAULT_SIZE.height / 2 : 0);
    const shardEnd = pathPoints(paths["s1->gather"])[0][forward];
    for (const [index, shard] of shards.entries()) {
      const mirror = shards[shards.length - 1 - index];
      // Top and bottom branches turn at the same point...
      expect(bend(paths[`load->${shard}`])).toBe(bend(paths[`load->${mirror}`]));
      expect(bend(paths[`${shard}->gather`])).toBe(bend(paths[`${mirror}->gather`]));
      // ...and the join mirrors the split around the column of shards.
      const straight = pathPoints(paths[`load->${shard}`]).length === 2;
      expect(pathPoints(paths[`${shard}->gather`]).length === 2).toBe(straight);
      if (straight) continue;
      expect(bend(paths[`load->${shard}`]) - sourceEnd).toBeCloseTo(
        targetStart - bend(paths[`${shard}->gather`]),
      );
    }
    expect(shardStart - sourceEnd).toBeCloseTo(targetStart - shardEnd);
  });
});

describe("assignColumns", () => {
  const segment = (hop: string, from: number, to: number) => ({ hop, from, to });

  it("lets verticals that stay apart, or fork from one spot, share a column", () => {
    expect(assignColumns([segment("up", 0, -100), segment("down", 50, 150)]).columns).toEqual(
      new Map([
        ["up", 0],
        ["down", 0],
      ]),
    );
    expect(assignColumns([segment("up", 0, -100), segment("down", 0, 100)]).columns).toEqual(
      new Map([
        ["up", 0],
        ["down", 0],
      ]),
    );
  });

  it("keeps overlapping verticals in their order, from the card they spread from", () => {
    const spreading = assignColumns([segment("outer", 0, -200), segment("inner", 10, -100)]);
    expect(spreading).toEqual({
      columns: new Map([
        ["outer", 0],
        ["inner", 1],
      ]),
      count: 2,
    });
    // The same pair converging bends next to the card it reaches.
    const converging = assignColumns([
      segment("inner", -100, 10),
      segment("outer", -200, 0),
      segment("lone", 200, 20),
    ]);
    expect(converging.columns).toEqual(
      new Map([
        ["inner", 0],
        ["outer", 1],
        ["lone", 1],
      ]),
    );
  });
});

describe("placeAtMedians", () => {
  it("snaps to the strongest pull instead of averaging pulls", () => {
    const pulls = [
      { value: 0, weight: 2 },
      { value: 100, weight: 1 },
    ];
    expect(placeAtMedians([pulls], [])).toEqual([0]);
    expect(
      placeAtMedians(
        [
          [
            { value: 0, weight: 1 },
            { value: 100, weight: 1 },
          ],
        ],
        [],
      ),
    ).toEqual([50]);
  });

  it("keeps the requested gaps between items pulled to one spot", () => {
    const pull = [{ value: 0, weight: 1 }];
    expect(placeAtMedians([pull, pull, pull], [10, 10])).toEqual([-10, 0, 10]);
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
