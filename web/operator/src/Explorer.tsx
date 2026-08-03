import {
  memo,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import type {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  RunSummaryMsg,
} from "./generated/operator";

export type Selection =
  | { kind: "workflow"; workflowId: string }
  | { kind: "run"; workflowId: string; runId: string };

interface ExplorerProps {
  catalog?: CatalogSnapshotMsg;
  runs: Record<string, RunSummaryMsg>;
  selection?: Selection;
  onSelect: (selection: Selection) => void;
}

const EMPTY_RUNS: RunSummaryMsg[] = [];
const RUN_ROW_HEIGHT = 44;
const RUN_ROW_OVERSCAN = 8;

interface WorkflowBranchProps {
  workflow: FlowInfoMsg;
  runs: RunSummaryMsg[];
  scrollElement: HTMLElement | null;
  contentLayoutRevision: number;
  selection?: Selection;
  onSelect: (selection: Selection) => void;
}

function branchSelection(selection: Selection | undefined, workflowId: string) {
  if (selection?.workflowId !== workflowId) return "";
  return selection.kind === "workflow" ? "workflow" : `run:${selection.runId}`;
}

function sameRuns(left: RunSummaryMsg[], right: RunSummaryMsg[]) {
  return left === right || (
    left.length === right.length &&
    left.every((summary, index) => summary === right[index])
  );
}

function statusLabel(status: string) {
  return status === "success" ? "✓" : status === "failed" ? "!" : status === "running" ? "●" : "·";
}

const WorkflowBranch = memo(function WorkflowBranch({
  workflow,
  runs,
  scrollElement,
  contentLayoutRevision,
  selection,
  onSelect,
}: WorkflowBranchProps) {
  const [expanded, setExpanded] = useState(true);
  const runList = useRef<HTMLDivElement>(null);
  const [scrollMargin, setScrollMargin] = useState(0);
  const virtualizer = useVirtualizer({
    count: expanded ? runs.length : 0,
    getScrollElement: () => scrollElement,
    estimateSize: () => RUN_ROW_HEIGHT,
    getItemKey: (index) => runs[index].runId,
    overscan: RUN_ROW_OVERSCAN,
    scrollMargin,
    initialRect: { width: 280, height: 800 },
  });

  useLayoutEffect(() => {
    if (!expanded || !runList.current || !scrollElement) return;
    const listRect = runList.current.getBoundingClientRect();
    const scrollRect = scrollElement.getBoundingClientRect();
    setScrollMargin(listRect.top - scrollRect.top + scrollElement.scrollTop);
  }, [contentLayoutRevision, expanded, runs.length, scrollElement]);


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
          {runs.length ? (
            <div
              ref={runList}
              className="run-virtual-list"
              role="list"
              aria-label={`Runs for ${workflow.displayName}`}
              style={{ height: virtualizer.getTotalSize() }}
            >
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const summary = runs[virtualRow.index];
                return (
                  <div
                    className="run-virtual-row"
                    data-index={virtualRow.index}
                    key={summary.runId}
                    role="listitem"
                    aria-setsize={runs.length}
                    aria-posinset={virtualRow.index + 1}
                    style={{
                      height: virtualRow.size,
                      transform: `translateY(${virtualRow.start - scrollMargin}px)`,
                    }}
                  >
                    <button
                      type="button"
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
                  </div>
                );
              })}
            </div>
          ) : (
            <span className="no-runs">No runs yet</span>
          )}
        </div>
      )}
    </div>
  );
}, (left, right) =>
  left.workflow === right.workflow &&
  left.scrollElement === right.scrollElement &&
  left.onSelect === right.onSelect &&
  left.contentLayoutRevision === right.contentLayoutRevision &&
  sameRuns(left.runs, right.runs) &&
  branchSelection(left.selection, left.workflow.workflowId) ===
    branchSelection(right.selection, right.workflow.workflowId),
);


function compareNewestRun(left: RunSummaryMsg, right: RunSummaryMsg) {
  const leftSequence = BigInt(left.createdSequence);
  const rightSequence = BigInt(right.createdSequence);
  if (leftSequence === rightSequence) return left.runId.localeCompare(right.runId);
  return leftSequence < rightSequence ? 1 : -1;
}

function ExplorerView({ catalog, runs, selection, onSelect }: ExplorerProps) {
  const [collapsedTargets, setCollapsedTargets] = useState<Record<string, true>>({});
  const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
  const [contentElement, setContentElement] = useState<HTMLDivElement | null>(null);
  const [contentLayoutRevision, setContentLayoutRevision] = useState(0);
  const targets = useMemo(() => {
    if (!catalog) return [];
    return catalog.scanTargets.length
      ? catalog.scanTargets
      : [
          {
            alias: "workflows",
            targetPath: "Configured workflows",
            kind: "directory",
          },
        ];
  }, [catalog]);
  const workflowsByTarget = useMemo(() => {
    if (!catalog) return {};
    return Object.fromEntries(
      targets.map((target) => [
        target.alias,
        target.alias === "workflows"
          ? catalog.workflows
          : catalog.workflows.filter((workflow) => workflow.rootAlias === target.alias),
      ]),
    );
  }, [catalog, targets]);
  const runsByWorkflow = useMemo(() => {
    const grouped: Record<string, RunSummaryMsg[]> = {};
    for (const summary of Object.values(runs)) {
      (grouped[summary.workflowId] ??= []).push(summary);
    }
    for (const summaries of Object.values(grouped)) summaries.sort(compareNewestRun);
    return grouped;
  }, [runs]);
  useLayoutEffect(() => {
    if (!contentElement || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      setContentLayoutRevision((revision) => revision + 1);
    });
    observer.observe(contentElement);
    return () => observer.disconnect();
  }, [contentElement]);
  if (!catalog) {
    return (
      <aside id="operator-explorer" className="explorer skeleton" aria-label="Explorer">
        <div />
        <div />
        <div />
      </aside>
    );
  }
  return (
    <aside
      id="operator-explorer"
      ref={setScrollElement}
      className="explorer"
      aria-label="Explorer"
    >
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
      <div className="target-list" ref={setContentElement}>
        {targets.map((target) => {
          const workflows = workflowsByTarget[target.alias] ?? [];
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
                      runs={runsByWorkflow[workflow.workflowId] ?? EMPTY_RUNS}
                      scrollElement={scrollElement}
                      contentLayoutRevision={contentLayoutRevision}
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

export const Explorer = memo(ExplorerView);
