import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useGraphOrientation } from "./useGraphOrientation";

let canvasWidth = 0;
const observed: (() => void)[] = [];

function Harness() {
  const { containerRef, orientation, toggleOrientation } = useGraphOrientation();
  return (
    <div ref={containerRef}>
      <output aria-label="Orientation">{orientation}</output>
      <button onClick={toggleOrientation}>Toggle</button>
    </div>
  );
}

function setCanvasWidth(width: number) {
  canvasWidth = width;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () => new DOMRect(0, 0, canvasWidth, 800),
  );
}

// Deliver a resize through every channel the canvas could listen on.
function resizeCanvas(width: number) {
  canvasWidth = width;
  act(() => {
    window.dispatchEvent(new Event("resize"));
    for (const notify of observed) notify();
  });
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class implements ResizeObserver {
      constructor(readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        observed.push(() => {
          const size = [{ inlineSize: canvasWidth, blockSize: 800 }];
          this.callback(
            [
              {
                target,
                contentRect: new DOMRect(0, 0, canvasWidth, 800),
                borderBoxSize: size,
                contentBoxSize: size,
                devicePixelContentBoxSize: size,
              },
            ],
            this,
          );
        });
      }
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  observed.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("graph orientation", () => {
  it.each([
    [1200, "horizontal"],
    [900, "horizontal"],
    [600, "vertical"],
  ])("starts from the %ipx canvas width as %s", (width, expected) => {
    setCanvasWidth(width);
    render(<Harness />);
    expect(screen.getByLabelText("Orientation")).toHaveTextContent(expected);
  });

  it("keeps the initial orientation when the canvas resizes afterwards", () => {
    setCanvasWidth(1200);
    render(<Harness />);
    resizeCanvas(600);
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
  });

  it("switches only when the user toggles it", () => {
    setCanvasWidth(600);
    render(<Harness />);
    fireEvent.click(screen.getByText("Toggle"));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    resizeCanvas(500);
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    fireEvent.click(screen.getByText("Toggle"));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("vertical");
  });
});
