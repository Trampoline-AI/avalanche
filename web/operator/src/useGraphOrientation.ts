import { useLayoutEffect, useRef, useState } from "react";

export type GraphOrientation = "horizontal" | "vertical";

const WIDE_CANVAS_WIDTH = 900;

// The canvas width picks the orientation once, when the canvas mounts. Later resizes
// (window changes, the inspector opening) never re-orient the graph; only the user does.
export function useGraphOrientation() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [orientation, setOrientation] = useState<GraphOrientation>("vertical");

  // Measure before paint so wide canvases never flash the vertical layout first.
  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const width = element.getBoundingClientRect().width;
    setOrientation(width >= WIDE_CANVAS_WIDTH ? "horizontal" : "vertical");
  }, []);

  const toggleOrientation = () =>
    setOrientation((current) => (current === "vertical" ? "horizontal" : "vertical"));

  return { containerRef, orientation, toggleOrientation };
}
