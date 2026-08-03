import { useCallback, useEffect, useRef, useState } from "react";

import type { OperatorApi } from "./api";
import { Explorer, type Selection } from "./Explorer";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { RunControls } from "./RunControls";
import { useOperatorProjection } from "./state";

export function App({ api }: { api: OperatorApi }) {
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [selection, setSelection] = useState<Selection>();
  const [inspectedNode, setInspectedNode] = useState<string>();
  const [explorerOpen, setExplorerOpen] = useState(false);
  const previousSelectedRunId = useRef(state.selectedRunId);

  useEffect(() => {
    const workflows = state.catalog?.workflows ?? [];
    if (!workflows.length) {
      if (selection) {
        setSelection(undefined);
        setInspectedNode(undefined);
        void selectRun(undefined);
      }
      return;
    }
    if (!selection) {
      setSelection({ kind: "workflow", workflowId: workflows[0].workflowId });
      return;
    }
    if (!workflows.some((workflow) => workflow.workflowId === selection.workflowId)) {
      setSelection({ kind: "workflow", workflowId: workflows[0].workflowId });
      setInspectedNode(undefined);
      void selectRun(undefined);
    }
  }, [selectRun, selection, state.catalog]);

  useEffect(() => {
    const priorSelectedRunId = previousSelectedRunId.current;
    previousSelectedRunId.current = state.selectedRunId;
    if (
      selection?.kind !== "run" ||
      state.selectedRunId !== undefined ||
      state.selectedRunStatus !== "idle" ||
      priorSelectedRunId !== selection.runId
    ) {
      return;
    }

    if (state.runs[selection.runId]) {
      void selectRun(selection.runId);
      return;
    }

    const workflows = state.catalog?.workflows ?? [];
    const workflow =
      workflows.find((item) => item.workflowId === selection.workflowId) ?? workflows[0];
    setSelection(
      workflow ? { kind: "workflow", workflowId: workflow.workflowId } : undefined,
    );
    setInspectedNode(undefined);
    void selectRun(undefined);
  }, [
    selectRun,
    selection,
    state.catalog,
    state.runs,
    state.selectedRunId,
    state.selectedRunStatus,
  ]);

  useEffect(
    () => () => {
      void selectRun(undefined);
    },
    [selectRun],
  );

  const workflow = state.catalog?.workflows.find(
    (item) => item.workflowId === selection?.workflowId,
  );
  const historical = selection?.kind === "run";
  const runSummary = historical ? state.runs[selection.runId] : undefined;
  const run =
    historical &&
    state.selectedRunId === selection.runId &&
    state.selectedRunStatus === "ready" &&
    state.selectedRun?.summary?.runId === selection.runId
      ? state.selectedRun
      : undefined;
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const closeNode = useCallback(() => setInspectedNode(undefined), []);
  const select = useCallback(
    (next: Selection) => {
      setSelection(next);
      setInspectedNode(undefined);
      setExplorerOpen(false);
      void selectRun(next.kind === "run" ? next.runId : undefined);
    },
    [selectRun],
  );

  const liveDescriptorKey =
    historical && inspectedNode ? `${selection.runId}:${inspectedNode}` : "";

  return (
    <div className={`app-shell ${explorerOpen ? "explorer-open" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            A
          </span>
          <div>
            <strong>Avalanche</strong>
            <span>Operator</span>
          </div>
        </div>
        <div className="breadcrumb">
          <span>{workflow?.rootAlias || "Local operator"}</span>
          {workflow && <><i>/</i><strong>{workflow.displayName}</strong></>}
          {historical && <><i>/</i><strong>{selection.runId}</strong></>}
        </div>
        <div className={`connection connection-${state.connection}`}>
          <span />
          {state.connection === "live" ? "Live" : state.connection}
          <small>seq {state.sequence}</small>
        </div>
        <button
          type="button"
          className="explorer-toggle"
          aria-controls="operator-explorer"
          aria-expanded={explorerOpen}
          onClick={() => setExplorerOpen((open) => !open)}
        >
          Explorer
        </button>
      </header>
      {state.error && <div className="connection-error">{state.error}</div>}
      <main className={`workspace ${inspectedNode && (!historical || run) ? "with-inspector" : ""}`}>
        <Explorer
          catalog={state.catalog}
          runs={state.runs}
          selection={selection}
          onSelect={select}
        />
        <section className="canvas-shell">
          <header className="view-header">
            <div>
              <span className="eyebrow">
                {historical ? "Historical run" : "Current definition"}
              </span>
              <h1>{historical ? selection.runId : workflow?.displayName || "Operator"}</h1>
              <p>
                {historical
                  ? `Recorded topology · ${runSummary?.status ?? "unknown"}`
                  : workflow
                    ? `${workflow.nodeIds.length} nodes · ${workflow.relativeFile}`
                    : "Waiting for a workflow catalog"}
              </p>
            </div>
            <RunControls
              workflow={!historical ? workflow : undefined}
              run={run}
              pending={state.action}
              onStart={startRun}
              onCancel={cancelRun}
            />
          </header>
          <div className={historical ? "canvas run-canvas" : "canvas blueprint-canvas"}>
            {historical ? (
              run ? (
                <>
                  <GraphCanvas
                    runTopology={run.topology}
                    runNodes={run.nodes}
                    onOpenNode={openNode}
                  />
                  <div className="historical-badge">
                    <span>Immutable run snapshot</span>
                    Current workflow changes do not alter this canvas
                  </div>
                </>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "loading" ? (
                <div className="empty-state" role="status">
                  <span>◇</span>
                  <h2>Loading run snapshot</h2>
                  <p>Retrieving the retained topology and execution state.</p>
                </div>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "error" ? (
                <div className="empty-state" role="alert">
                  <span>!</span>
                  <h2>Run snapshot unavailable</h2>
                  <p>{state.selectedRunError || "The selected run could not be loaded."}</p>
                </div>
              ) : (
                <div className="empty-state">
                  <span>◇</span>
                  <h2>No run snapshot</h2>
                  <p>Select the run again to load its retained topology.</p>
                </div>
              )
            ) : workflow ? (
              <GraphCanvas workflow={workflow} onOpenNode={openNode} />
            ) : (
              <div className="empty-state">
                <span>◇</span>
                <h2>No workflows discovered</h2>
                <p>Catalog changes will appear here as the operator scans configured targets.</p>
              </div>
            )}
          </div>
        </section>
        {inspectedNode && (!historical || run) && (
          <Inspector
            api={api}
            workflow={workflow}
            run={run}
            nodeId={inspectedNode}
            liveEvents={state.liveEvents[liveDescriptorKey]}
            liveLogs={historical ? state.liveLogs[liveDescriptorKey] : undefined}
            onClose={closeNode}
          />
        )}
      </main>
    </div>
  );
}
