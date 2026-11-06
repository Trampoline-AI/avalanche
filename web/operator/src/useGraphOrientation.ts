import { useLayoutEffect, useRef, useState } from "react";

export type GraphOrientation = "horizontal" | "vertical";

export interface CanvasSize {
  width: number;
  height: number;
}

const WIDE_CANVAS_WIDTH = 900;

// React Flow sizes its pane the same way (offset box), so a fit computed from this
// size matches the fit React Flow computes.
function sizeOf(element: HTMLElement): CanvasSize {
  return { width: element.offsetWidth, height: element.offsetHeight };
}

// The canvas width picks the orientation once, when the canvas mounts. Later resizes
// (window changes, the inspector opening) never re-orient the graph; only the user does.
// The canvas size is tracked too, so a graph mounted later (a run switch to another
// topology) can size its first frame for the canvas as it is then.
export function useGraphOrientation() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [orientation, setOrientation] = useState<GraphOrientation>("vertical");
  // Undefined until measured, which happens before the first paint.
  const [canvasSize, setCanvasSize] = useState<CanvasSize>();

  // Measure before paint so wide canvases never flash the vertical layout first.
  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const width = element.getBoundingClientRect().width;
    setOrientation(width >= WIDE_CANVAS_WIDTH ? "horizontal" : "vertical");
    setCanvasSize(sizeOf(element));
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const next = sizeOf(element);
      setCanvasSize((current) =>
        current?.width === next.width && current.height === next.height ? current : next,
      );
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const toggleOrientation = () =>
    setOrientation((current) => (current === "vertical" ? "horizontal" : "vertical"));

  return { containerRef, orientation, toggleOrientation, canvasSize };
}
