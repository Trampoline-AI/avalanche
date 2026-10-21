import { useEffect, useRef, useState } from "react";

export type GraphOrientation = "horizontal" | "vertical";

const STORAGE_KEY = "avalanche.graph-orientation-lock";
const WIDE_CANVAS_WIDTH = 900;

function savedLock(): GraphOrientation | undefined {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "horizontal" || value === "vertical" ? value : undefined;
  } catch {
    // Storage can be unavailable in embedded/private browser contexts.
    return undefined;
  }
}

function saveLock(value: GraphOrientation | undefined) {
  try {
    if (value) window.localStorage.setItem(STORAGE_KEY, value);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The control still works for this mounted canvas without browser storage.
  }
}

export function useGraphOrientation() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [automatic, setAutomatic] = useState<GraphOrientation>("vertical");
  const [lockedOrientation, setLockedOrientation] = useState(savedLock);
  const [manual, setManual] = useState<GraphOrientation>();

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    let lastAutomatic: GraphOrientation | undefined;
    const resize = (width: number) => {
      if (width <= 0) return;
      const next = width < WIDE_CANVAS_WIDTH ? "vertical" : "horizontal";
      if (next !== lastAutomatic) {
        lastAutomatic = next;
        setAutomatic(next);
        setManual(undefined);
      }
    };
    resize(element.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) resize(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const orientation = lockedOrientation ?? manual ?? automatic;
  const chooseOrientation = (next: GraphOrientation) => {
    setManual(next);
    if (lockedOrientation) {
      setLockedOrientation(next);
      saveLock(next);
    }
  };
  const toggleLock = () => {
    const next = lockedOrientation ? undefined : orientation;
    setLockedOrientation(next);
    setManual(undefined);
    saveLock(next);
  };

  return {
    containerRef,
    orientation,
    locked: lockedOrientation !== undefined,
    chooseOrientation,
    toggleLock,
  };
}
