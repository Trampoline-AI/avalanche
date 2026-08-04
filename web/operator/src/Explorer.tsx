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
      className={selected ? "tree-select active" : "tree-select"}
      onClick={() => onSelect({ kind: "workflow", workflowId: workflow.workflowId })}
    >
      <span className="workflow-glyph">◇</span>
      <span>
        <strong>{workflow.displayName}</strong>
        <small>{workflow.relativeFile}</small>
      </span>
    </button>
  );
});

function ExplorerView({ catalog, selection, onSelect, onCollapse }: ExplorerProps) {
  const collapseButton = onCollapse ? (
    <button
      type="button"
      className="explorer-collapse-button"
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
      <aside id="operator-explorer" className="explorer skeleton" aria-label="Explorer">
        {collapseButton}
        <div />
        <div />
        <div />
      </aside>
    );
  }
  return (
    <aside id="operator-explorer" className="explorer" aria-label="Explorer">
      <header>
        <span className="eyebrow">Navigator</span>
        <h2>Explorer</h2>
        <span className="catalog-revision">catalog r{catalog.revision}</span>
        {collapseButton}
      </header>
      {catalog.diagnostics.length > 0 && (
        <details className="diagnostics" open>
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
      <div className="workflow-list">
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
          <span className="workflow-list-empty">No workflows scanned</span>
        )}
      </div>
    </aside>
  );
}

export const Explorer = memo(ExplorerView);
