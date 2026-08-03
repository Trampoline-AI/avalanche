import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import "@testing-library/jest-dom/vitest";

const viewportGeometry = [
  { selector: ".explorer", width: 280, height: 800 },
  { selector: ".turn-list", width: 640, height: 220 },
  { selector: ".log-list", width: 640, height: 220 },
] as const;

function geometryFor(element: Element) {
  return viewportGeometry.find(({ selector }) => element.matches(selector));
}

const nativeGetBoundingClientRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
  const geometry = geometryFor(this);
  return geometry
    ? new DOMRect(0, 0, geometry.width, geometry.height)
    : nativeGetBoundingClientRect.call(this);
};

for (const [property, dimension] of [
  ["clientWidth", "width"],
  ["clientHeight", "height"],
  ["offsetWidth", "width"],
  ["offsetHeight", "height"],
] as const) {
  const nativeDescriptor = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    property,
  );
  Object.defineProperty(HTMLElement.prototype, property, {
    configurable: true,
    get() {
      const geometry = geometryFor(this);
      return geometry?.[dimension] ?? nativeDescriptor?.get?.call(this) ?? 0;
    },
  });
}

class VirtualViewportResizeObserver implements ResizeObserver {
  readonly #callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.#callback = callback;
  }

  observe(target: Element, _options?: ResizeObserverOptions) {
    const geometry = geometryFor(target);
    if (!geometry) return;
    const size = {
      blockSize: geometry.height,
      inlineSize: geometry.width,
    };
    this.#callback(
      [
        {
          target,
          borderBoxSize: [size],
          contentBoxSize: [size],
          contentRect: new DOMRect(0, 0, geometry.width, geometry.height),
          devicePixelContentBoxSize: [size],
        } as ResizeObserverEntry,
      ],
      this,
    );
  }

  unobserve() {}

  disconnect() {}
}

Object.defineProperty(window, "ResizeObserver", {
  configurable: true,
  value: VirtualViewportResizeObserver,
  writable: true,
});
globalThis.ResizeObserver = VirtualViewportResizeObserver;

afterEach(cleanup);
