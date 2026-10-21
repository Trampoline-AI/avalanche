import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useGraphOrientation } from "./useGraphOrientation";

let resize: (width: number) => void;
const disconnect = vi.fn();

function Harness() {
  const { containerRef, orientation, locked, chooseOrientation, toggleLock } =
    useGraphOrientation();
  return (
    <div ref={containerRef}>
      <output aria-label="Orientation">{orientation}</output>
      <button onClick={() => chooseOrientation("horizontal")}>Horizontal</button>
      <button onClick={() => chooseOrientation("vertical")}>Vertical</button>
      <button aria-pressed={locked} onClick={toggleLock}>
        Lock
      </button>
    </div>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  disconnect.mockClear();
  vi.stubGlobal(
    "ResizeObserver",
    class implements ResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        this.callback = callback;
      }
      readonly callback: ResizeObserverCallback;
      observe(target: Element) {
        resize = (width) =>
          this.callback(
            [
              {
                target,
                contentRect: new DOMRect(0, 0, width, 800),
                borderBoxSize: [{ inlineSize: width, blockSize: 800 }],
                contentBoxSize: [{ inlineSize: width, blockSize: 800 }],
                devicePixelContentBoxSize: [{ inlineSize: width, blockSize: 800 }],
              },
            ],
            this,
          );
      }
      unobserve() {}
      disconnect = disconnect;
    },
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("responsive orientation preference", () => {
  it("adapts to canvas width and retains manual choice until a breakpoint changes", () => {
    render(<Harness />);
    act(() => resize(1100));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    fireEvent.click(screen.getByText("Vertical"));
    act(() => resize(1000));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("vertical");
    act(() => resize(700));
    act(() => resize(1100));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    act(() => resize(700));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("vertical");
  });

  it("locks across resizing and remounts, allows switching while locked, and unlocks to auto", () => {
    const view = render(<Harness />);
    act(() => resize(600));
    fireEvent.click(screen.getByText("Horizontal"));
    fireEvent.click(screen.getByText("Lock"));
    act(() => resize(1200));
    act(() => resize(600));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    view.unmount();
    expect(disconnect).toHaveBeenCalled();
    render(<Harness />);
    act(() => resize(600));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    expect(screen.getByText("Lock")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByText("Vertical"));
    act(() => resize(1200));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("vertical");
    fireEvent.click(screen.getByText("Lock"));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
  });

  it("keeps controls usable when browser storage is denied", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("Denied", "SecurityError");
    });
    render(<Harness />);
    act(() => resize(600));
    fireEvent.click(screen.getByText("Horizontal"));
    fireEvent.click(screen.getByText("Lock"));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("horizontal");
    fireEvent.click(screen.getByText("Lock"));
    expect(screen.getByLabelText("Orientation")).toHaveTextContent("vertical");
  });
});
