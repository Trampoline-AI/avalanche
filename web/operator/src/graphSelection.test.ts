import { describe, expect, it } from "vitest";
import { createGraphLayout } from "./graphLayout";
import { highlightConnectedEdges } from "./graphSelection";

const topology = {
  nodeIds: ["root", "before", "selected", "after", "end", "sibling", "other"],
  graph: {
    root: { children: ["before", "selected", "sibling"] },
    before: { children: ["selected"] },
    selected: { children: ["after"] },
    after: { children: ["end"] },
    sibling: { children: ["end"] },
    other: { children: [] },
  },
};

describe.each(["horizontal", "vertical"] as const)(
  "%s connection highlights",
  (orientation) => {
    it("highlights only direct connections in blue and dashes all other edges", () => {
      const { edges } = createGraphLayout(topology, orientation);
      const highlighted = highlightConnectedEdges(edges, "selected");
      expect(
        highlighted
          .filter((edge) => edge.className?.includes("connected"))
          .map((edge) => edge.id),
      ).toEqual(["root->selected", "before->selected", "selected->after"]);
      for (const edge of highlighted) {
        const connected = edge.source === "selected" || edge.target === "selected";
        expect(edge.style).toMatchObject({
          stroke: connected ? "var(--color-acid)" : "var(--color-edge)",
          strokeDasharray: connected ? undefined : "6 4",
        });
        expect(edge.markerEnd).toMatchObject({ color: edge.style?.stroke });
      }
      expect(edges.every((edge) => !edge.style)).toBe(true);
      const selectedBranch = highlighted.find((edge) => edge.id === "root->selected")!;
      const siblingBranch = highlighted.find((edge) => edge.id === "root->sibling")!;
      expect(selectedBranch.zIndex).toBeGreaterThan(siblingBranch.zIndex!);
      expect(siblingBranch.style?.strokeDasharray).toBe("6 4");
    });

    it("restores solid gray edges on deselection and updates direct neighbors on reselection", () => {
      const { edges } = createGraphLayout(topology, orientation);
      expect(
        highlightConnectedEdges(edges).every(
          (edge) =>
            edge.style?.stroke === "var(--color-edge)" &&
            edge.style.strokeDasharray === undefined,
        ),
      ).toBe(true);
      expect(
        highlightConnectedEdges(edges, "other").every(
          (edge) => edge.style?.strokeDasharray === "6 4",
        ),
      ).toBe(true);
      expect(
        highlightConnectedEdges(edges, "root")
          .filter((edge) => edge.className?.includes("connected"))
          .map((edge) => edge.id),
      ).toEqual(["root->before", "root->selected", "root->sibling"]);
    });
  },
);
