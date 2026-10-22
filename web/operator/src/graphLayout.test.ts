import { describe, expect, it } from "vitest";
import { createGraphLayout, graphPositions } from "./graphLayout";

const topology = {
  nodeIds: ["source", "left", "right", "sink"],
  graph: {
    source: { children: ["left", "right", "sink"] },
    left: { children: ["sink"] },
    right: { children: ["sink"] },
  },
};

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
      const { rows, edges } = createGraphLayout(workflow, orientation);
      expect(rows).toEqual({
        0: ["collect"],
        1: ["normalize", "instructions"],
        2: ["propose"],
      });
      const positions = graphPositions(rows, new Map(), orientation);
      const forward = orientation === "vertical" ? "y" : "x";
      const across = orientation === "vertical" ? "x" : "y";
      expect(positions.instructions[forward]).toBe(positions.normalize[forward]);
      expect(positions.instructions[across]).toBeGreaterThan(positions.normalize[across]);
      expect(edges.find((edge) => edge.source === "instructions")?.type).toBe("step");
      expect(edges.map(({ source, target }) => [source, target])).toEqual([
        ["collect", "normalize"],
        ["instructions", "propose"],
        ["normalize", "propose"],
      ]);
      expect(workflow).toEqual(original);
    },
  );

  it("keeps shared inputs before their earliest consumer and leaves unused roots at the start", () => {
    const { rows, edges } = createGraphLayout(
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
    expect(rows[0]).toEqual(["start", "early_input", "unused"]);
    expect(rows[1]).toEqual(["prepare", "later_input"]);
    expect(rows[2]).toEqual(["first"]);
    expect(rows[3]).toEqual(["last"]);
    const positions = graphPositions(rows, new Map(), "vertical");
    for (const edge of edges) {
      expect(positions[edge.source].y).toBeLessThan(positions[edge.target].y);
    }
  });

  it.each(["horizontal", "vertical"] as const)(
    "preserves branches, joins, and skip dependencies in %s layout",
    (orientation) => {
      const { rows, edges } = createGraphLayout(topology, orientation);
      expect(edges).toHaveLength(5);
      const positions = graphPositions(rows, new Map(), orientation);
      const forward = orientation === "vertical" ? "y" : "x";
      const across = orientation === "vertical" ? "x" : "y";
      for (const edge of edges) {
        expect(positions[edge.target][forward]).toBeGreaterThan(
          positions[edge.source][forward],
        );
      }
      expect(positions.left[forward]).toBe(positions.right[forward]);
      expect(positions.left[across]).not.toBe(positions.right[across]);
      const direct = edges.find((edge) => edge.id === "left->sink")!;
      expect([direct.sourceHandle, direct.targetHandle]).toEqual(
        orientation === "vertical"
          ? ["source-bottom", "target-top"]
          : ["source-right", "target-left"],
      );
      const skip = edges.find((edge) => edge.id === "source->sink")!;
      expect(skip.type).toBe("skip");
      expect([skip.sourceHandle, skip.targetHandle]).toEqual(
        orientation === "vertical"
          ? ["source-bottom", "target-top"]
          : ["source-right", "target-left"],
      );
      expect(skip.data).toMatchObject({ orientation });
      const fanOut = edges.filter((edge) => edge.source === "source");
      expect(fanOut.filter((edge) => edge.type === "skip").map((edge) => edge.target)).toEqual([
        "sink",
      ]);
      expect(
        fanOut.filter((edge) => edge.type === "branch").map((edge) => edge.target),
      ).toEqual(["left", "right"]);
      expect(skip.data?.nextRowIds).toEqual(["left", "right"]);
      expect(skip.data?.intermediateRows).toEqual([["left", "right"]]);
      expect(new Set(fanOut.map((edge) => edge.sourceHandle)).size).toBe(1);
      expect(fanOut.find((edge) => edge.target === "left")?.data?.targetRowIds).toEqual([
        "left",
        "right",
      ]);
    },
  );

  it.each(["horizontal", "vertical"] as const)(
    "keeps unrelated trunks separate and preserves every dependency in %s layout",
    (orientation) => {
      const { edges } = createGraphLayout(
        {
          nodeIds: ["a", "b", "c", "d", "e"],
          graph: {
            a: { children: ["b", "c", "d", "e", "e"] },
            b: { children: ["c", "d", "e"] },
            c: { children: ["d"] },
            d: { children: ["e"] },
          },
        },
        orientation,
      );
      expect(edges).toHaveLength(9);
      const fromA = edges.filter((edge) => edge.source === "a" && edge.type === "skip");
      const fromB = edges.filter((edge) => edge.source === "b" && edge.type === "skip");
      expect(new Set(fromA.map((edge) => edge.data?.lane)).size).toBe(1);
      expect(new Set(fromB.map((edge) => edge.data?.lane)).size).toBe(1);
      expect(fromA[0].data?.lane).not.toBe(fromB[0].data?.lane);
      expect(edges.find((edge) => edge.source === "c")?.type).toBe("step");
    },
  );

  it.each(["horizontal", "vertical"] as const)(
    "branches locally when all destinations are on the next level in %s layout",
    (orientation) => {
      const { edges } = createGraphLayout(
        {
          nodeIds: ["source", "left", "right"],
          graph: { source: { children: ["left", "right"] } },
        },
        orientation,
      );
      expect(edges.every((edge) => edge.type === "branch")).toBe(true);
      expect(edges.every((edge) => edge.data?.orientation === orientation)).toBe(true);
    },
  );

  it("keeps tall measured cards and parallel branches apart vertically", () => {
    const { rows } = createGraphLayout(topology, "vertical");
    const measurements = new Map([
      ["source", { width: 360, height: 400 }],
      ["left", { width: 360, height: 600 }],
      ["right", { width: 420, height: 130 }],
      ["sink", { width: 360, height: 250 }],
    ]);
    const positions = graphPositions(rows, measurements, "vertical");
    expect(positions.left.y - 300).toBeGreaterThan(positions.source.y + 200);
    expect(positions.sink.y - 125).toBeGreaterThan(positions.left.y + 300);
    expect(positions.right.x).toBeGreaterThan(positions.left.x + 360);
  });
});
