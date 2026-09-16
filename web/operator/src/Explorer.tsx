import { memo, useEffect, useState } from "react";
import { Folder, PanelLeft, X } from "lucide-react";

import type { CatalogSnapshotMsg, FlowInfoMsg } from "./model";
import type { OperatorUiSelection } from "./ui/types";

interface ExplorerProps {
  catalog?: CatalogSnapshotMsg;
  selection?: OperatorUiSelection;
  onSelect: (selection: OperatorUiSelection) => void;
  onTogglePin?: () => void;
  onHoverStart?: () => void;
  onHoverEnd?: () => void;
  open?: boolean;
  pinned?: boolean;
  expanded?: boolean;
}

interface WorkflowRowProps {
  workflow: FlowInfoMsg;
  selected: boolean;
  onSelect: (selection: OperatorUiSelection) => void;
}

const WorkflowRow = memo(function WorkflowRow({
  workflow,
  selected,
  onSelect,
}: WorkflowRowProps) {
  return (
    <button
      type="button"
      className={`tree-select block w-full min-w-0 cursor-pointer rounded-none border-0 p-2 text-left text-sm transition-colors hover:bg-canvas focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-sidebar-primary ${
        selected
          ? "active bg-canvas font-medium text-sidebar-primary"
          : "bg-transparent text-muted hover:text-ink"
      }`}
      aria-current={selected ? "true" : undefined}
      title={`${workflow.displayName}\n${workflow.relativeFile}`}
      onClick={() => onSelect({ kind: "workflow", workflowId: workflow.workflowId })}
    >
      <span className="block truncate leading-5">{workflow.displayName}</span>
      <small className="block truncate text-[10px] leading-3 font-normal text-secondary">
        {workflow.relativeFile}
      </small>
    </button>
  );
});

function diagnosticKey(catalog: CatalogSnapshotMsg): string {
  return catalog.diagnostics
    .filter((diagnostic) => diagnostic.kind !== "skipped")
    .map(
      (diagnostic) => `${diagnostic.kind}\u0000${diagnostic.path}\u0000${diagnostic.message}`,
    )
    .join("\u0001");
}

function ExplorerView({
  catalog,
  selection,
  onSelect,
  onTogglePin,
  onHoverStart,
  onHoverEnd,
  open = false,
  pinned = true,
  expanded = true,
}: ExplorerProps) {
  const [dismissedDiagnosticKey, setDismissedDiagnosticKey] = useState<string>();
  const visibleDiagnostics =
    catalog?.diagnostics.filter((diagnostic) => diagnostic.kind !== "skipped") ?? [];
  const currentDiagnosticKey = catalog ? diagnosticKey(catalog) : "";
  const diagnosticsDismissed = dismissedDiagnosticKey === currentDiagnosticKey;
  const contentVisible = expanded || open;
  const scanTarget = catalog?.scanTargets.length === 1 ? catalog.scanTargets[0] : undefined;
  const scanTargetLabel = scanTarget
    ? scanTarget.targetPath
        .replace(/[\\/]+$/, "")
        .split(/[\\/]/)
        .pop() ||
      scanTarget.alias ||
      "Scan target"
    : undefined;

  useEffect(() => {
    if (!currentDiagnosticKey) {
      setDismissedDiagnosticKey(undefined);
    }
  }, [currentDiagnosticKey]);

  return (
    <aside
      id="operator-explorer"
      className={`explorer relative col-start-1 row-start-1 flex h-full w-[var(--workspace-explorer-visible-width)] min-w-0 flex-col overflow-hidden border-r border-line bg-panel transition-[width,box-shadow] duration-200 ease-out motion-reduce:transition-none ${
        !pinned ? "z-30" : "z-20"
      } ${expanded && !pinned ? "shadow-[18px_0_45px_rgba(20,31,26,.16)]" : ""} max-[700px]:fixed max-[700px]:top-[58px] max-[700px]:bottom-0 max-[700px]:left-0 max-[700px]:z-[31] max-[700px]:w-[min(320px,100vw)] max-[700px]:shadow-[18px_0_45px_rgba(20,31,26,.16)] ${
        open ? "max-[700px]:flex" : "max-[700px]:hidden"
      }`}
      aria-label="Explorer"
      onMouseEnter={onHoverStart}
      onMouseLeave={onHoverEnd}
    >
      {contentVisible &&
        (catalog ? (
          <div className="min-h-0 min-w-[var(--workspace-explorer-width)] flex-1 overflow-auto">
            {visibleDiagnostics.length > 0 && !diagnosticsDismissed && (
              <details
                className="diagnostics relative m-2 rounded-lg border border-[#ead1a2] bg-[#fff8eb] p-[9px] text-[10px] [&>summary]:cursor-pointer [&>summary]:pr-6 [&>summary]:text-amber [&>div]:mt-[9px] [&>div]:border-t [&>div]:border-[#ead1a2] [&>div]:pt-2 [&_strong]:block [&_span]:block [&_span]:overflow-hidden [&_span]:text-ellipsis [&_span]:font-mono [&_span]:text-[8px] [&_span]:text-[#8b7655] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[#735b37]"
                open
              >
                <summary>
                  {visibleDiagnostics.length} reload issue
                  {visibleDiagnostics.length === 1 ? "" : "s"}
                </summary>
                <button
                  type="button"
                  className="absolute top-1.5 right-1.5 grid size-5 cursor-pointer place-items-center rounded text-amber hover:bg-[#f7e8c8] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-amber"
                  aria-label="Dismiss reload issues"
                  onClick={() => setDismissedDiagnosticKey(currentDiagnosticKey)}
                >
                  <X aria-hidden="true" className="size-3" strokeWidth={2} />
                </button>
                {visibleDiagnostics.map((diagnostic) => (
                  <div key={`${diagnostic.path}-${diagnostic.kind}`}>
                    <strong>{diagnostic.kind.replaceAll("_", " ")}</strong>
                    <span>{diagnostic.path}</span>
                    <p>{diagnostic.message}</p>
                  </div>
                ))}
              </details>
            )}
            <section className="p-2" aria-label={scanTargetLabel ?? "Workflows"}>
              {scanTarget && (
                <div
                  className="flex h-8 min-w-0 items-center gap-2 px-2 text-sm text-muted"
                  title={scanTarget.targetPath}
                >
                  <Folder aria-hidden="true" className="size-4 shrink-0" />
                  <span className="truncate">{scanTargetLabel}</span>
                </div>
              )}
              <ul
                className={`workflow-list flex min-w-0 flex-col gap-2 ${
                  scanTarget ? "ml-4 pl-2" : ""
                }`}
              >
                {catalog.workflows.map((workflow) => (
                  <li
                    key={workflow.workflowId}
                    className={`relative min-w-0 ${
                      scanTarget
                        ? "before:pointer-events-none before:absolute before:-left-2 before:top-[18px] before:w-2 before:-translate-y-1/2 before:border-t before:border-line after:pointer-events-none after:absolute after:-bottom-2 after:-left-2 after:top-0 after:border-l after:border-line first:after:-top-2 last:after:bottom-[calc(100%-18px)]"
                        : ""
                    }`}
                  >
                    <WorkflowRow
                      workflow={workflow}
                      selected={selection?.workflowId === workflow.workflowId}
                      onSelect={onSelect}
                    />
                  </li>
                ))}
              </ul>
              {catalog.workflows.length === 0 && (
                <p className="workflow-list-empty px-2 py-2.5 text-sm text-muted">
                  No workflows scanned
                </p>
              )}
            </section>
          </div>
        ) : (
          <div className="skeleton min-w-[var(--workspace-explorer-width)] flex-1 overflow-auto p-2 [&>div]:mb-2 [&>div]:h-12 [&>div]:animate-pulse [&>div]:bg-canvas motion-reduce:[&>div]:animate-none">
            <div />
            <div />
            <div />
          </div>
        ))}
      {onTogglePin && (
        <footer className="mt-auto flex h-12 shrink-0 items-center justify-start bg-panel px-2 max-[700px]:hidden">
          <button
            type="button"
            className={`flex h-8 cursor-pointer items-center rounded-md border-0 bg-transparent text-muted transition-colors hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid ${
              pinned ? "w-8 justify-center p-0" : "w-full justify-start gap-2 px-2"
            }`}
            aria-label={pinned ? "Collapse Explorer" : "Pin Explorer open"}
            aria-controls="operator-explorer"
            aria-expanded={pinned}
            onClick={onTogglePin}
          >
            <PanelLeft aria-hidden="true" className="size-4 shrink-0" />
            {!pinned && (
              <span
                className={`whitespace-nowrap text-sm transition-opacity duration-150 ${
                  expanded ? "opacity-100 delay-200" : "opacity-0 delay-0"
                }`}
              >
                Pin open
              </span>
            )}
          </button>
        </footer>
      )}
    </aside>
  );
}

export const Explorer = memo(ExplorerView);
