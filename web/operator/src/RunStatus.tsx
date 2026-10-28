import { useEffect, useRef } from "react";

import { subscribeRunningGlyph } from "./glyphClock";

type Tone = "active" | "success" | "danger" | "neutral";
const statusTones: Record<string, Tone> = {
  admitted: "active",
  running: "active",
  resumable: "active",
  success: "success",
  succeeded: "success",
  failed: "danger",
  rejected: "danger",
  integrity: "danger",
};
const toneClasses: Record<Tone, string> = {
  active: "text-run-status-active",
  success: "text-run-status-success",
  danger: "text-run-status-danger",
  neutral: "text-run-status-muted",
};

export function RunStatus({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const tone = Object.hasOwn(statusTones, normalized) ? statusTones[normalized] : "neutral";
  const glyph =
    tone === "active"
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
    if (tone !== "active" || !marker.current) return;
    return subscribeRunningGlyph(marker.current);
  }, [tone]);
  return (
    <span
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
      />
      {status}
    </span>
  );
}
