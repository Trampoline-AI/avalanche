import { useState } from "react";

import type {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  RunSnapshotMsg,
  ScanTargetMsg,
} from "./generated/operator";

export type Selection =
  | { kind: "workflow"; workflowId: string }
  | { kind: "run"; workflowId: string; runId: string };

interface ExplorerProps {
  catalog?: CatalogSnapshotMsg;
  runs: Record<string, RunSnapshotMsg>;
  selection?: Selection;
  onSelect: (selection: Selection) => void;
}

function statusLabel(status: string) {
  return status === "success" ? "✓" : status === "failed" ? "!" : status === "running" ? "●" : "·";
}

function WorkflowBranch({
  workflow,
  runs,
  selection,
  onSelect,
}: {
  workflow: FlowInfoMsg;
  runs: RunSnapshotMsg[];
  selection?: Selection;
  onSelect: (selection: Selection) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  return (
    <div className="workflow-branch">
      <div className="tree-row">
        <button
          type="button"
          className="tree-disclosure"
          onClick={() => setExpanded((value) => !value)}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${workflow.displayName}`}
        >
          {expanded ? "⌄" : "›"}
        </button>
        <button
          type="button"
          className={
            selection?.kind === "workflow" && selection.workflowId === workflow.workflowId
              ? "tree-select active"
              : "tree-select"
          }
          onClick={() => onSelect({ kind: "workflow", workflowId: workflow.workflowId })}
        >
          <span className="workflow-glyph">◇</span>
          <span>
            <strong>{workflow.displayName}</strong>
            <small>{workflow.relativeFile}</small>
          </span>
        </button>
      </div>
      {expanded && (
        <div className="run-branches">
          {runs.map((run) => {
            const summary = run.summary!;
            return (
              <button
                type="button"
                key={summary.runId}
                className={
                  selection?.kind === "run" && selection.runId === summary.runId
                    ? "run-select active"
                    : "run-select"
                }
                onClick={() =>
                  onSelect({
                    kind: "run",
                    workflowId: workflow.workflowId,
                    runId: summary.runId,
                  })
                }
              >
                <span className={`run-dot status-${summary.status}`}>
                  {statusLabel(summary.status)}
                </span>
                <span>
                  <strong>{summary.runId}</strong>
                  <small>
                    {summary.startedAt
                      ? `Created at sequence ${summary.createdSequence}`
                      : "Awaiting start"}
                  </small>
                </span>
              </button>
            );
          })}
          {!runs.length && <span className="no-runs">No runs yet</span>}
        </div>
      )}
    </div>
  );
}

function targetWorkflows(catalog: CatalogSnapshotMsg, target: ScanTargetMsg) {
  return catalog.workflows.filter((workflow) => workflow.rootAlias === target.alias);
}

export function Explorer({ catalog, runs, selection, onSelect }: ExplorerProps) {
  const [collapsedTargets, setCollapsedTargets] = useState<Record<string, true>>({});
  if (!catalog) {
    return (
      <aside id="operator-explorer" className="explorer skeleton" aria-label="Explorer">
        <div />
        <div />
        <div />
      </aside>
    );
  }
  const targets = catalog.scanTargets.length
    ? catalog.scanTargets
    : [
        {
          alias: "workflows",
          targetPath: "Configured workflows",
          kind: "directory",
        },
      ];
  return (
    <aside id="operator-explorer" className="explorer" aria-label="Explorer">
      <header>
        <span className="eyebrow">Navigator</span>
        <h2>Explorer</h2>
        <span className="catalog-revision">catalog r{catalog.revision}</span>
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
      <div className="target-list">
        {targets.map((target) => {
          const workflows =
            target.alias === "workflows"
              ? catalog.workflows
              : targetWorkflows(catalog, target);
          const collapsed = Boolean(collapsedTargets[target.alias]);
          return (
            <section className="target" key={target.alias}>
              <button
                type="button"
                className="target-heading"
                onClick={() =>
                  setCollapsedTargets((current) => {
                    const next = { ...current };
                    if (next[target.alias]) delete next[target.alias];
                    else next[target.alias] = true;
                    return next;
                  })
                }
              >
                <span>{collapsed ? "›" : "⌄"}</span>
                <span className="target-kind">{target.kind === "file" ? "F" : "D"}</span>
                <span>
                  <strong>{target.alias}</strong>
                  <small title={target.targetPath}>{target.targetPath}</small>
                </span>
              </button>
              {!collapsed && (
                <div className="workflow-list">
                  {workflows.map((workflow) => (
                    <WorkflowBranch
                      key={workflow.workflowId}
                      workflow={workflow}
                      runs={Object.values(runs)
                        .filter((run) => run.summary?.workflowId === workflow.workflowId)
                        .sort(
                          (left, right) =>
                            Number(right.summary!.createdSequence) -
                            Number(left.summary!.createdSequence),
                        )}
                      selection={selection}
                      onSelect={onSelect}
                    />
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </aside>
  );
}
