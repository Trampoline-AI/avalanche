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
      const direct = edges.find((edge) => edge.id === "source->left")!;
      expect([direct.sourceHandle, direct.targetHandle]).toEqual(
        orientation === "vertical"
          ? ["source-bottom", "target-top"]
          : ["source-right", "target-left"],
      );
      const skip = edges.find((edge) => edge.id === "source->sink")!;
      expect(skip.type).toBe("skip");
      expect([skip.sourceHandle, skip.targetHandle]).toEqual(
        orientation === "vertical"
          ? ["source-right", "target-right"]
          : ["source-bottom", "target-bottom"],
      );
      expect(skip.data).toMatchObject({ orientation });
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
