// Preserve the runtime status verbatim; tones never project or infer a status.
const statusTones: Record<string, string> = {
  failed: "danger",
  rejected: "danger",
  integrity: "danger",
  success: "success",
  succeeded: "success",
  running: "active",
  admitted: "active",
  resumable: "active",
  requesting: "waiting",
  submitted: "waiting",
  waiting: "waiting",
  queued: "waiting",
  pending: "waiting",
  parked: "waiting",
};

const toneClasses: Record<string, string> = {
  danger: "border-danger text-danger",
  success: "border-ink text-ink",
  active: "border-acid bg-status-active text-acid",
  waiting: "border-amber text-amber",
  neutral: "border-line text-muted",
};

export function RunStatusBadge({ status }: { status: string }) {
  const tone = Object.hasOwn(statusTones, status.toLowerCase())
    ? statusTones[status.toLowerCase()]
    : "neutral";
  return (
    <span
      data-status={status}
      data-tone={tone}
      className={`run-status-text inline-flex h-5 shrink-0 items-center gap-1.5 rounded border px-1.5 font-mono text-[9px] font-normal tracking-[0.06em] uppercase ${toneClasses[tone]}`}
    >
      <span aria-hidden="true" className="size-1.5 bg-current" />
      {status}
    </span>
  );
}
