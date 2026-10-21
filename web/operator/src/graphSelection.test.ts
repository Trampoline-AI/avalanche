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
    it("traces ancestors and descendants, including skip edges, without highlighting sibling branches", () => {
      const { edges } = createGraphLayout(topology, orientation);
      const highlighted = highlightConnectedEdges(edges, "selected");
      expect(
        highlighted
          .filter((edge) => edge.className?.includes("upstream"))
          .map((edge) => edge.id),
      ).toEqual(["root->before", "root->selected", "before->selected"]);
      expect(
        highlighted
          .filter((edge) => edge.className?.includes("downstream"))
          .map((edge) => edge.id),
      ).toEqual(["selected->after", "after->end"]);
      const upstream = highlighted.find((edge) => edge.id === "root->selected")!;
      const downstream = highlighted.find((edge) => edge.id === "selected->after")!;
      expect(upstream.type).toBe("skip");
      expect(upstream.style?.stroke).not.toBe(downstream.style?.stroke);
      expect(upstream.markerEnd).toMatchObject({ color: upstream.style?.stroke });
      expect(downstream.markerEnd).toMatchObject({ color: downstream.style?.stroke });
      expect(highlighted.find((edge) => edge.id === "sibling->end")?.style?.strokeWidth).toBe(
        1.4,
      );
      expect(edges.every((edge) => !edge.style)).toBe(true);
    });

    it("clears highlights on deselection and recomputes them for a different node", () => {
      const { edges } = createGraphLayout(topology, orientation);
      for (const selected of [undefined, "other"]) {
        expect(
          highlightConnectedEdges(edges, selected).every(
            (edge) => edge.style?.strokeWidth === 1.4,
          ),
        ).toBe(true);
      }
      expect(
        highlightConnectedEdges(edges, "root").every((edge) =>
          edge.className?.includes("downstream"),
        ),
      ).toBe(true);
    });
  },
);
