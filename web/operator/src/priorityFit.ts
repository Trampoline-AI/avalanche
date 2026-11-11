import { type RefObject, useEffect, useMemo, useState } from "react";

/**
 * Progressive disclosure without truncation: a row's fields are listed most
 * important first; when the row narrows, the least important optional fields
 * drop out entirely instead of ellipsizing. An optional field is kept only while
 * every more important field is kept too, so fields drop strictly in reverse
 * priority order. Required fields are never dropped.
 */
export interface PriorityField {
  id: string;
  /** Natural single-line width in px. */
  width: number;
  /** Narrowest width the field may wrap to (two lines); defaults to `width`. */
  minWidth?: number;
  /** Never dropped (e.g. status, time ago). */
  required?: boolean;
  /** Rendered against the previous field with no gap, e.g. " (36.4s)" after "3 min ago". */
  attached?: boolean;
}

export interface PriorityFit {
  /** Ids of the kept fields. */
  visible: ReadonlySet<string>;
  /** Kept fields that need their wrapped (two-line) width to fit. */
  wrapped: ReadonlySet<string>;
}

function rowWidth(fields: readonly PriorityField[], gap: number, natural: boolean) {
  let total = 0;
  let items = 0;
  for (const field of fields) {
    total += natural ? field.width : (field.minWidth ?? field.width);
    if (!field.attached) items += 1;
  }
  return total + Math.max(0, items - 1) * gap;
}

/** Which fields (in priority order, most important first) fit `available` px. */
export function fitPriorityFields(
  available: number,
  fields: readonly PriorityField[],
  gap = 0,
): PriorityFit {
  const kept = fields.filter((field) => field.required);
  for (const field of fields) {
    if (field.required) continue;
    if (rowWidth([...kept, field], gap, false) > available) break;
    kept.push(field);
  }
  const wrapped = new Set<string>();
  if (rowWidth(kept, gap, true) > available) {
    for (const field of kept)
      if ((field.minWidth ?? field.width) < field.width) wrapped.add(field.id);
  }
  return { visible: new Set(kept.map((field) => field.id)), wrapped };
}

/** True when `id` should render: before the first measurement every field shows. */
export function priorityFieldVisible(fit: PriorityFit | undefined, id: string) {
  return !fit || fit.visible.has(id);
}

export type TextMeasure = (text: string) => number;

let measureContext: OffscreenCanvasRenderingContext2D | null = null;

/** Text width in px for a CSS `font`, or undefined where canvas text metrics are unavailable. */
export function textMeasure(font: string): TextMeasure | undefined {
  if (!measureContext && typeof OffscreenCanvas !== "undefined") {
    measureContext = new OffscreenCanvas(1, 1).getContext("2d");
  }
  const context = measureContext;
  if (!context) return undefined;
  return (text) => {
    context.font = font;
    return context.measureText(text).width;
  };
}

/** The narrowest width at which `text` wraps to at most two lines. */
export function twoLineWidth(text: string, measure: TextMeasure): number {
  const words = text.split(" ");
  if (words.length > 1) {
    let best = Infinity;
    for (let index = 1; index < words.length; index += 1) {
      const first = measure(words.slice(0, index).join(" "));
      const rest = measure(words.slice(index).join(" "));
      best = Math.min(best, Math.max(first, rest));
    }
    return Math.ceil(best) + 1;
  }
  // One word breaks anywhere (overflow-wrap: anywhere): half, plus a glyph of slack.
  return Math.ceil(measure(text) / 2 + measure("W")) + 1;
}

// One ResizeObserver for every observed element on the page.
const widthListeners = new Map<Element, (width: number) => void>();
let resizeObserver: ResizeObserver | undefined;

function observeWidth(element: Element, listener: (width: number) => void) {
  if (typeof ResizeObserver === "undefined") return () => undefined;
  resizeObserver ??= new ResizeObserver((entries) => {
    for (const entry of entries) widthListeners.get(entry.target)?.(entry.contentRect.width);
  });
  widthListeners.set(element, listener);
  resizeObserver.observe(element);
  return () => {
    widthListeners.delete(element);
    resizeObserver?.unobserve(element);
  };
}

/** The element's content-box width from the shared ResizeObserver (undefined until measured). */
export function useObservedWidth(
  ref: RefObject<Element | null>,
  /** Re-attach when the observed element mounts or remounts. */
  active = true,
): number | undefined {
  const [width, setWidth] = useState<number>();
  useEffect(() => {
    const element = ref.current;
    if (!active || !element) return;
    return observeWidth(element, setWidth);
  }, [active, ref]);
  return width;
}

export interface PriorityTextField {
  id: string;
  text: string;
  required?: boolean;
  attached?: boolean;
  /** May wrap to two lines instead of dropping (never ellipsized). */
  wrap?: boolean;
}

/**
 * Fits text fields (most important first) into `ref`'s width, using its computed
 * font. Returns undefined until measured, so every field renders by default.
 * Lay the kept fields out in one flex row with `gap` px between them.
 */
export function usePriorityFit(
  ref: RefObject<HTMLElement | null>,
  fields: readonly PriorityTextField[],
  gap = 0,
): PriorityFit | undefined {
  const width = useObservedWidth(ref);
  const key = JSON.stringify(fields);
  return useMemo(() => {
    const element = ref.current;
    if (!width || !element) return undefined;
    const measure = textMeasure(getComputedStyle(element).font);
    if (!measure) return undefined;
    const parsed = JSON.parse(key) as PriorityTextField[];
    return fitPriorityFields(
      width,
      parsed.map((field) => {
        const natural = Math.ceil(measure(field.text)) + 1;
        return {
          id: field.id,
          width: natural,
          minWidth: field.wrap
            ? Math.min(natural, twoLineWidth(field.text, measure))
            : undefined,
          required: field.required,
          attached: field.attached,
        };
      }),
      gap,
    );
  }, [gap, key, ref, width]);
}
