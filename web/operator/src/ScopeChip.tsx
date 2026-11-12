/** A state-entry-qualified step ID (`e<entry>.<step>`) of a machine run's state entry. */
export function stateEntryStep(nodeId: string): { entry: number; step: string } | undefined {
  const match = /^v(\d+)\.(.+)$/.exec(nodeId);
  return match ? { entry: Number(match[1]), step: match[2] } : undefined;
}

/** What a bottom-pane tab is narrowed to, with the way back to everything. */
export interface PaneScope {
  /** E.g. "Step: apply_nits · entry 2" or "State: amend". */
  label: string;
  /** Absent: the scope shows without a clear button. */
  onClear?: () => void;
}

/**
 * A step's scope label: its display name without a machine state entry's `e<N>.`
 * prefix, then the state entry (`Step: apply_nits · entry 2`).
 */
export function stepScopeLabel(nodeId: string, displayNames?: Record<string, string>): string {
  const entry = stateEntryStep(nodeId);
  const named = displayNames?.[nodeId] || nodeId;
  const step = stateEntryStep(named)?.step ?? named;
  return entry ? `Step: ${step} · entry ${entry.entry}` : `Step: ${step}`;
}

/**
 * The row at the top of a bottom-pane tab's content: what the tab shows ("All
 * steps", "All states") or the scope it is narrowed to, with a clear button.
 */
export function ScopeChip({ scope, all }: { scope?: PaneScope; all: string }) {
  return (
    <div className="run-pane-scope flex min-w-0 items-center border-b border-line px-[9px] py-1 font-mono text-[9px]">
      {scope ? (
        <span
          className="run-pane-scope-chip inline-flex min-w-0 items-center gap-1 rounded border border-acid bg-panel py-px pr-px pl-1.5 text-ink"
          data-scoped="true"
        >
          <span className="min-w-0 truncate">{scope.label}</span>
          {scope.onClear && (
            <button
              type="button"
              className="grid size-4 flex-none cursor-pointer place-items-center rounded-sm border-0 bg-transparent p-0 text-acid hover:bg-line focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-acid"
              aria-label={`Clear scope (${scope.label})`}
              title="Clear"
              onClick={scope.onClear}
            >
              <span aria-hidden="true">×</span>
            </button>
          )}
        </span>
      ) : (
        <span className="run-pane-scope-chip text-secondary" data-scoped="false">
          {all}
        </span>
      )}
    </div>
  );
}
