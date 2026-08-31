import type { KeyboardEvent, PointerEvent } from "react";
import { useRef } from "react";

const PANEL_KEYBOARD_STEP = 16;

interface WorkspaceDividerProps {
  className?: string;
  label: string;
  controls: string;
  value: number;
  min: number;
  max: number;
  pointerDirection: 1 | -1;
  onChange: (value: number) => void;
}

export function WorkspaceDivider({
  className = "",
  label,
  controls,
  value,
  min,
  max,
  pointerDirection,
  onChange,
}: WorkspaceDividerProps) {
  const dragStart = useRef<{ clientX: number; value: number } | undefined>(undefined);

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragStart.current) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragStart.current = undefined;
  };

  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    let next: number | undefined;
    if (event.key === "ArrowLeft") {
      next = value - PANEL_KEYBOARD_STEP * pointerDirection;
    }
    if (event.key === "ArrowRight") {
      next = value + PANEL_KEYBOARD_STEP * pointerDirection;
    }
    if (event.key === "Home") next = min;
    if (event.key === "End") next = max;
    if (next === undefined) return;
    event.preventDefault();
    onChange(Math.min(max, Math.max(min, next)));
  };

  return (
    <div
      className={`workspace-divider relative z-[4] min-h-0 min-w-0 cursor-col-resize touch-none bg-panel after:absolute after:inset-y-0 after:left-1/2 after:w-0.5 after:-translate-x-1/2 after:bg-line after:content-[''] after:transition-colors after:duration-150 hover:after:bg-acid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid focus-visible:after:bg-acid ${className}`}
      role="separator"
      aria-label={label}
      aria-controls={controls}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      aria-valuetext={`${value} pixels`}
      tabIndex={0}
      onKeyDown={resizeWithKeyboard}
      onPointerDown={(event) => {
        event.preventDefault();
        dragStart.current = { clientX: event.clientX, value };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        const start = dragStart.current;
        if (!start) return;
        const next = start.value + (event.clientX - start.clientX) * pointerDirection;
        onChange(Math.min(max, Math.max(min, next)));
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    />
  );
}
