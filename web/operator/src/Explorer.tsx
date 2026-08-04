import { memo } from "react";

import type { CatalogSnapshotMsg, FlowInfoMsg } from "./generated/operator";

export type Selection =
  | { kind: "workflow"; workflowId: string }
  | { kind: "run"; workflowId: string; runId: string };

interface ExplorerProps {
  catalog?: CatalogSnapshotMsg;
  selection?: Selection;
  onSelect: (selection: Selection) => void;
  onCollapse?: () => void;
  open?: boolean;
  collapsed?: boolean;
}

interface WorkflowRowProps {
  workflow: FlowInfoMsg;
  selected: boolean;
  onSelect: (selection: Selection) => void;
}

const WorkflowRow = memo(function WorkflowRow({
  workflow,
  selected,
  onSelect,
}: WorkflowRowProps) {
  return (
    <button
      type="button"
      className={`tree-select grid w-full min-w-0 cursor-pointer grid-cols-[23px_minmax(0,1fr)] gap-1 rounded-[7px] border-0 bg-transparent p-2 text-left hover:bg-[#f1f4f2] [&_strong]:block [&_strong]:overflow-hidden [&_strong]:text-ellipsis [&_strong]:whitespace-nowrap [&_strong]:text-[11px] [&_strong]:font-semibold [&_small]:mt-[3px] [&_small]:block [&_small]:overflow-hidden [&_small]:text-ellipsis [&_small]:whitespace-nowrap [&_small]:text-[8px] [&_small]:text-secondary ${selected ? "active bg-[#f1f4f2] shadow-[inset_2px_0_#2563eb]" : ""}`}
      onClick={() => onSelect({ kind: "workflow", workflowId: workflow.workflowId })}
    >
      <span className="workflow-glyph text-base text-acid">◇</span>
      <span>
        <strong>{workflow.displayName}</strong>
        <small>{workflow.relativeFile}</small>
      </span>
    </button>
  );
});

function ExplorerView({ catalog, selection, onSelect, onCollapse, open = false, collapsed = false }: ExplorerProps) {
  const collapseButton = onCollapse ? (
    <button
      type="button"
      className="explorer-collapse-button absolute top-4 right-3.5 grid size-7 cursor-pointer place-items-center rounded-[7px] border border-line bg-white p-0 text-xl leading-none text-secondary hover:border-secondary hover:bg-[#f7f9f8] hover:text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid max-[700px]:hidden"
      aria-label="Collapse Explorer"
      aria-controls="operator-explorer"
      aria-expanded="true"
      onClick={onCollapse}
    >
      <span aria-hidden="true">‹</span>
    </button>
  ) : null;
  if (!catalog) {
    return (
      <aside id="operator-explorer" className={`explorer skeleton col-start-1 min-w-0 overflow-auto border-r border-line bg-panel p-5 max-[700px]:fixed max-[700px]:top-[58px] max-[700px]:bottom-0 max-[700px]:left-0 max-[700px]:z-[31] max-[700px]:w-[min(320px,100vw)] max-[700px]:shadow-[18px_0_45px_rgba(20,31,26,.16)] [&>div]:mb-[9px] [&>div]:h-[38px] [&>div]:animate-pulse [&>div]:rounded-[7px] [&>div]:bg-[#edf1ef] ${collapsed ? "invisible overflow-hidden border-r-0 max-[700px]:visible max-[700px]:overflow-auto max-[700px]:border-r" : ""} ${open ? "max-[700px]:block" : "max-[700px]:hidden"}`} aria-label="Explorer">
        {collapseButton}
        <div />
        <div />
        <div />
      </aside>
    );
  }
  return (
    <aside id="operator-explorer" className={`explorer col-start-1 min-w-0 overflow-auto border-r border-line bg-panel max-[700px]:fixed max-[700px]:top-[58px] max-[700px]:bottom-0 max-[700px]:left-0 max-[700px]:z-[31] max-[700px]:w-[min(320px,100vw)] max-[700px]:shadow-[18px_0_45px_rgba(20,31,26,.16)] ${collapsed ? "invisible overflow-hidden border-r-0 max-[700px]:visible max-[700px]:overflow-auto max-[700px]:border-r" : ""} ${open ? "max-[700px]:block" : "max-[700px]:hidden"}`} aria-label="Explorer">
      <header className="relative px-[18px] pt-[22px] pb-3.5">
        <span className="eyebrow block font-mono text-[9px] tracking-[.16em] text-acid uppercase">Navigator</span>
        <h2 className="mt-[5px] text-[17px]">Explorer</h2>
        <span className="catalog-revision absolute right-[18px] bottom-[17px] font-mono text-[9px] text-secondary">catalog r{catalog.revision}</span>
        {collapseButton}
      </header>
      {catalog.diagnostics.length > 0 && (
        <details className="diagnostics mx-3 mb-3 rounded-lg border border-[#ead1a2] bg-[#fff8eb] p-[9px] text-[10px] [&>summary]:cursor-pointer [&>summary]:text-amber [&>div]:mt-[9px] [&>div]:border-t [&>div]:border-[#ead1a2] [&>div]:pt-2 [&_strong]:block [&_span]:block [&_span]:overflow-hidden [&_span]:text-ellipsis [&_span]:font-mono [&_span]:text-[8px] [&_span]:text-[#8b7655] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[#735b37]" open>
          <summary>{catalog.diagnostics.length} reload issue{catalog.diagnostics.length === 1 ? "" : "s"}</summary>
          {catalog.diagnostics.map((diagnostic) => (
            <div key={`${diagnostic.path}-${diagnostic.kind}`}>
              <strong>{diagnostic.kind.replaceAll("_", " ")}</strong>
              <span>{diagnostic.path}</span>
              <p>{diagnostic.message}</p>
            </div>
          ))}
        </details>
      )}
      <div className="workflow-list mx-2.5 mt-0 border-t border-line px-0 pt-2 pb-[30px]">
        {catalog.workflows.map((workflow) => (
          <WorkflowRow
            key={workflow.workflowId}
            workflow={workflow}
            selected={
              selection?.kind === "workflow" &&
              selection.workflowId === workflow.workflowId
            }
            onSelect={onSelect}
          />
        ))}
        {catalog.workflows.length === 0 && (
          <span className="workflow-list-empty block px-2 py-2.5 font-mono text-[9px] text-secondary">No workflows scanned</span>
        )}
      </div>
    </aside>
  );
}

export const Explorer = memo(ExplorerView);
