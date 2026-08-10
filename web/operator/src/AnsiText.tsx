import { type CSSProperties, type ReactNode } from "react";

interface AnsiState {
  foreground?: string;
  background?: string;
  bold: boolean;
  dim: boolean;
  italic: boolean;
  underline: boolean;
  inverse: boolean;
  strikethrough: boolean;
}

interface AnsiSegment {
  text: string;
  state: AnsiState;
}

const SGR_SEQUENCE = /\u001B\[([0-9;]*)m/g;
const ANSI_COLORS = [
  "#1f2937",
  "#c43d36",
  "#16805d",
  "#a15c00",
  "#2563eb",
  "#a855f7",
  "#0f766e",
  "#dfe4e1",
  "#64748b",
  "#ef4444",
  "#22c55e",
  "#f59e0b",
  "#3b82f6",
  "#c084fc",
  "#14b8a6",
  "#ffffff",
] as const;

function initialState(): AnsiState {
  return {
    foreground: undefined,
    background: undefined,
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    inverse: false,
    strikethrough: false,
  };
}

function isByte(value: number | undefined): value is number {
  return value !== undefined && value >= 0 && value <= 255;
}

function rgb(red: number, green: number, blue: number): string {
  return `rgb(${red}, ${green}, ${blue})`;
}

function xtermColor(value: number): string {
  if (value < ANSI_COLORS.length) return ANSI_COLORS[value];
  if (value < 232) {
    const index = value - 16;
    const levels = [0, 95, 135, 175, 215, 255];
    return rgb(
      levels[Math.floor(index / 36)],
      levels[Math.floor((index % 36) / 6)],
      levels[index % 6],
    );
  }
  const gray = 8 + (value - 232) * 10;
  return rgb(gray, gray, gray);
}

function parameters(raw: string): number[] {
  if (!raw) return [0];
  return raw.split(";").flatMap((part) => {
    const value = Number(part);
    return Number.isInteger(value) && value >= 0 ? [value] : [];
  });
}

function applySgr(state: AnsiState, values: number[]): AnsiState {
  const next = { ...state };
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (value === 0) {
      Object.assign(next, initialState());
    } else if (value === 1) {
      next.bold = true;
    } else if (value === 2) {
      next.dim = true;
    } else if (value === 3) {
      next.italic = true;
    } else if (value === 4) {
      next.underline = true;
    } else if (value === 7) {
      next.inverse = true;
    } else if (value === 9) {
      next.strikethrough = true;
    } else if (value === 22) {
      next.bold = false;
      next.dim = false;
    } else if (value === 23) {
      next.italic = false;
    } else if (value === 24) {
      next.underline = false;
    } else if (value === 27) {
      next.inverse = false;
    } else if (value === 29) {
      next.strikethrough = false;
    } else if (value === 39) {
      next.foreground = undefined;
    } else if (value === 49) {
      next.background = undefined;
    } else if (value >= 30 && value <= 37) {
      next.foreground = ANSI_COLORS[value - 30];
    } else if (value >= 40 && value <= 47) {
      next.background = ANSI_COLORS[value - 40];
    } else if (value >= 90 && value <= 97) {
      next.foreground = ANSI_COLORS[value - 90 + 8];
    } else if (value >= 100 && value <= 107) {
      next.background = ANSI_COLORS[value - 100 + 8];
    } else if (value === 38 || value === 48) {
      const target = value === 38 ? "foreground" : "background";
      const mode = values[index + 1];
      if (mode === 5 && isByte(values[index + 2])) {
        next[target] = xtermColor(values[index + 2]);
        index += 2;
      } else if (
        mode === 2 &&
        isByte(values[index + 2]) &&
        isByte(values[index + 3]) &&
        isByte(values[index + 4])
      ) {
        next[target] = rgb(values[index + 2], values[index + 3], values[index + 4]);
        index += 4;
      }
    }
  }
  return next;
}

function segments(text: string): AnsiSegment[] {
  const result: AnsiSegment[] = [];
  let state = initialState();
  let start = 0;
  for (const match of text.matchAll(SGR_SEQUENCE)) {
    if (match.index > start) result.push({ text: text.slice(start, match.index), state });
    state = applySgr(state, parameters(match[1]));
    start = match.index + match[0].length;
  }
  if (start < text.length) result.push({ text: text.slice(start), state });
  return result;
}

function styleFor(state: AnsiState): CSSProperties | undefined {
  const foreground = state.inverse ? (state.background ?? "#ffffff") : state.foreground;
  const background = state.inverse ? (state.foreground ?? "#17211c") : state.background;
  if (
    !foreground &&
    !background &&
    !state.bold &&
    !state.dim &&
    !state.italic &&
    !state.underline &&
    !state.strikethrough
  ) {
    return undefined;
  }
  return {
    color: foreground,
    backgroundColor: background,
    fontWeight: state.bold ? 700 : undefined,
    opacity: state.dim ? 0.7 : undefined,
    fontStyle: state.italic ? "italic" : undefined,
    textDecoration:
      [state.underline ? "underline" : "", state.strikethrough ? "line-through" : ""]
        .filter(Boolean)
        .join(" ") || undefined,
  };
}

export function AnsiText({ text }: { text: string }): ReactNode {
  return segments(text).map((segment, index) => {
    const style = styleFor(segment.state);
    return style ? (
      <span key={index} style={style}>
        {segment.text}
      </span>
    ) : (
      segment.text
    );
  });
}
