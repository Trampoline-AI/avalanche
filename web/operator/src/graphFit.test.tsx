import type { Node } from "@xyflow/react";
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  bestMachineFit,
  firstFrameFor,
  reserveFit,
  reserveSide,
  useFitViewOptions,
  type CanvasReserve,
  type MachineFitCandidate,
} from "./GraphCanvas";
import { MIN_ARC_LABEL_PX } from "./machineGraph";

// The runs panel floating over the canvas's top-left corner.
const PANEL: CanvasReserve = { width: 330, height: 300 };
const OPTIONS = { padding: 0.24 };
const MIN_ZOOM = 0.12;

const row = (count: number): Node[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `n${index}`,
    position: { x: index * 400, y: 0 },
    data: {},
  }));
const column = (count: number): Node[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `n${index}`,
    position: { x: 0, y: index * 200 },
    data: {},
  }));

const zoomOn = (
  nodes: Node[],
  canvas: { width: number; height: number },
  side: "beside" | "below",
) => firstFrameFor(nodes, canvas, reserveFit(OPTIONS, PANEL, side), MIN_ZOOM)!.viewport.zoom;

describe("fit beside or below the runs panel", () => {
  it("fits a wide, short graph below the panel and a tall one beside it", () => {
    const canvas = { width: 1200, height: 900 };
    expect(reserveSide(OPTIONS, PANEL, row(6), canvas, MIN_ZOOM)).toBe("below");
    expect(reserveSide(OPTIONS, PANEL, column(6), canvas, MIN_ZOOM)).toBe("beside");
  });

  it("keeps the graph clear of the panel on the side it picked", () => {
    const below = reserveFit(OPTIONS, PANEL, "below").padding as Record<string, string>;
    expect(parseFloat(below.top)).toBeGreaterThanOrEqual(PANEL.height! + 16);
    const beside = reserveFit(OPTIONS, PANEL, "beside").padding as Record<string, string>;
    expect(parseFloat(beside.left)).toBeGreaterThanOrEqual(PANEL.width + 16);
  });

  it("stays beside unless below is more than 5% larger, so small changes do not flip it", () => {
    const nodes = row(4);
    let near = 0;
    let clear = 0;
    for (let height = 300; height <= 1400; height += 5) {
      const canvas = { width: 1000, height };
      const ratio = zoomOn(nodes, canvas, "below") / zoomOn(nodes, canvas, "beside");
      const side = reserveSide(OPTIONS, PANEL, nodes, canvas, MIN_ZOOM);
      if (ratio > 1 && ratio <= 1.05) {
        near++;
        expect(side).toBe("beside");
      } else if (ratio > 1.05) {
        clear++;
        expect(side).toBe("below");
      } else expect(side).toBe("beside");
    }
    // Both bands were crossed.
    expect(near).toBeGreaterThan(0);
    expect(clear).toBeGreaterThan(0);
  });

  it("fits beside a panel whose height is unknown", () => {
    expect(
      reserveSide(OPTIONS, { width: 330 }, row(6), { width: 1200, height: 900 }, 0.12),
    ).toBe("beside");
  });
});

describe("arc label scale against the fitted zoom", () => {
  const rendered = (fit: MachineFitCandidate) => 10 * fit.scale * (fit.zoom ?? 0);

  it("grows the labels until they render at the minimum size when the zoom allows it", () => {
    // Gaps widen with the labels, so the zoom drops a little per step.
    const fit = bestMachineFit(
      (scale) => ({ scale, zoom: 0.8 / (1 + (scale - 1) * 0.1) }),
      0.12,
    );
    expect(fit.scale).toBeGreaterThan(1);
    expect(rendered(fit)).toBeGreaterThanOrEqual(MIN_ARC_LABEL_PX - 0.05);
  });

  it("stops at the zoom floor and keeps the whole graph with the largest labels", () => {
    const calls: number[] = [];
    // A small canvas: larger labels shrink the graph to the floor, where it no
    // longer fits whole.
    const fit = bestMachineFit((scale) => {
      calls.push(scale);
      return { scale, zoom: Math.max(MIN_ZOOM, 0.3 / scale ** 1.6) };
    }, MIN_ZOOM);
    expect(fit.zoom!).toBeGreaterThan(MIN_ZOOM * 1.01);
    expect(calls.length).toBeLessThanOrEqual(7);
    // No whole-graph candidate it tried rendered its labels larger.
    for (const scale of calls) {
      const zoom = Math.max(MIN_ZOOM, 0.3 / scale ** 1.6);
      if (zoom > MIN_ZOOM * 1.01)
        expect(rendered({ scale, zoom })).toBeLessThanOrEqual(rendered(fit));
    }
  });

  it("returns the plain fit when the canvas is not measured yet", () => {
    expect(bestMachineFit((scale) => ({ scale, zoom: undefined }), MIN_ZOOM)).toEqual({
      scale: 1,
      zoom: undefined,
    });
  });
});

describe("fit options identity", () => {
  it("re-fits on a new label scale and keeps the options otherwise", () => {
    const reserve = { width: 330, height: 300 };
    const { result, rerender } = renderHook(
      ({ scale }: { scale: number }) => useFitViewOptions(OPTIONS, reserve, "below", scale),
      { initialProps: { scale: 1 } },
    );
    const first = result.current;
    rerender({ scale: 1 });
    expect(result.current).toBe(first);
    rerender({ scale: 1.4 });
    expect(result.current).not.toBe(first);
    expect(result.current).toEqual(first);
  });
});
