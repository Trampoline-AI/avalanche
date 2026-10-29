import { useEffect, useRef } from "react";

import { subscribeRunningGlyph } from "./glyphClock";

export type StatusTone = "active" | "warning" | "success" | "danger" | "neutral";
type Tone = StatusTone;
// Avalanche run/node states and Delta step states share one glyph set.
// Anything not listed is neutral (○), so a node never renders without a glyph.
const statusTones: Record<string, Tone> = {
  admitted: "active",
  running: "active",
  resumable: "active",
  ready: "warning",
  waiting: "warning",
  success: "success",
  succeeded: "success",
  accepted: "success",
  failed: "danger",
  rejected: "danger",
  integrity: "danger",
  indeterminate: "danger",
  expired: "danger",
};

export function statusTone(status: string | undefined): StatusTone {
  const normalized = (status ?? "").toLowerCase();
  return Object.hasOwn(statusTones, normalized) ? statusTones[normalized] : "neutral";
}

export const statusToneColor: Record<StatusTone, string> = {
  active: "var(--color-run-status-active)",
  warning: "var(--color-amber)",
  success: "var(--color-run-status-success)",
  danger: "var(--color-run-status-danger)",
  neutral: "var(--color-run-status-muted)",
};
const toneClasses: Record<Tone, string> = {
  active: "text-run-status-active",
  success: "text-run-status-success",
  danger: "text-run-status-danger",
  warning: "text-amber",
  neutral: "text-run-status-muted",
};

export function RunStatus({
  status,
  glyphOnly = false,
}: {
  status: string;
  glyphOnly?: boolean;
}) {
  const normalized = status.toLowerCase();
  const tone = statusTone(status);
  const glyph =
    tone === "active" || tone === "warning"
      ? "◐"
      : tone === "success"
        ? "✓"
        : tone === "danger"
          ? "✗"
          : normalized === "cancelled"
            ? "⊘"
            : "○";
  const marker = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if ((tone !== "active" && tone !== "warning") || !marker.current) return;
    return subscribeRunningGlyph(marker.current);
  }, [tone]);
  return (
    <span
      aria-label={glyphOnly ? status : undefined}
      data-status={status}
      data-tone={tone}
      className={`run-status-text inline-flex items-center gap-1.5 whitespace-nowrap font-mono font-normal tracking-[0.06em] uppercase ${toneClasses[tone]}`}
    >
      <span
        ref={marker}
        aria-hidden="true"
        className="run-status-glyph"
        data-glyph={tone === "active" ? "running" : tone}
        data-char={glyph}
      >
        {glyph}
      </span>
      {!glyphOnly && status}
    </span>
  );
}
