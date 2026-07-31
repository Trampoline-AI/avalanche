import { useCallback, useEffect, useMemo, useState } from "react";

import type { OperatorApi } from "./api";
import { Explorer, type Selection } from "./Explorer";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { RunControls } from "./RunControls";
import { useOperatorProjection } from "./state";

export function App({ api }: { api: OperatorApi }) {
  const { state, startRun, cancelRun } = useOperatorProjection(api);
  const [selection, setSelection] = useState<Selection>();
  const [inspectedNode, setInspectedNode] = useState<string>();

  useEffect(() => {
    const workflows = state.catalog?.workflows ?? [];
    if (!workflows.length) {
      setSelection(undefined);
      return;
    }
    if (!selection) {
      setSelection({ kind: "workflow", workflowId: workflows[0].workflowId });
      return;
    }
    if (!workflows.some((workflow) => workflow.workflowId === selection.workflowId)) {
      setSelection({ kind: "workflow", workflowId: workflows[0].workflowId });
    }
  }, [selection, state.catalog]);

  const workflow = state.catalog?.workflows.find(
    (item) => item.workflowId === selection?.workflowId,
  );
  const run = selection?.kind === "run" ? state.runs[selection.runId] : undefined;
  const latestRun = useMemo(
    () =>
      Object.values(state.runs)
        .filter((item) => item.summary?.workflowId === workflow?.workflowId)
        .sort(
          (left, right) =>
            Number(right.summary!.createdSequence) - Number(left.summary!.createdSequence),
        )[0],
    [state.runs, workflow?.workflowId],
  );
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const select = useCallback((next: Selection) => {
    setSelection(next);
    setInspectedNode(undefined);
  }, []);

  const selectedRun = run ?? (selection?.kind === "workflow" ? latestRun : undefined);
  const liveEventKey = run && inspectedNode ? `${run.summary?.runId}:${inspectedNode}` : "";

  return (
    <div className="app-shell">
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
          {run?.summary && <><i>/</i><strong>{run.summary.runId}</strong></>}
        </div>
        <div className={`connection connection-${state.connection}`}>
          <span />
          {state.connection === "live" ? "Live" : state.connection}
          <small>seq {state.sequence}</small>
        </div>
      </header>
      {state.error && <div className="connection-error">{state.error}</div>}
      <main className={`workspace ${inspectedNode ? "with-inspector" : ""}`}>
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
                {run ? "Historical run" : "Current definition"}
              </span>
              <h1>{run?.summary?.runId || workflow?.displayName || "Operator"}</h1>
              <p>
                {run
                  ? `Recorded topology · ${run.summary?.status ?? "unknown"}`
                  : workflow
                    ? `${workflow.nodeIds.length} nodes · ${workflow.relativeFile}`
                    : "Waiting for a workflow catalog"}
              </p>
            </div>
            <RunControls
              workflow={!run ? workflow : undefined}
              run={run ?? selectedRun}
              pending={state.action}
              onStart={startRun}
              onCancel={cancelRun}
            />
          </header>
          <div className={run ? "canvas run-canvas" : "canvas blueprint-canvas"}>
            {workflow || run?.topology ? (
              <GraphCanvas
                workflow={run ? undefined : workflow}
                runTopology={run?.topology}
                runNodes={run?.nodes}
                onOpenNode={openNode}
              />
            ) : (
              <div className="empty-state">
                <span>◇</span>
                <h2>No workflows discovered</h2>
                <p>Catalog changes will appear here as the operator scans configured targets.</p>
              </div>
            )}
            {run && (
              <div className="historical-badge">
                <span>Immutable run snapshot</span>
                Current workflow changes do not alter this canvas
              </div>
            )}
          </div>
        </section>
        {inspectedNode && (
          <Inspector
            api={api}
            workflow={workflow}
            run={run}
            nodeId={inspectedNode}
            liveEvents={state.liveEvents[liveEventKey]}
            liveLogs={run?.summary ? state.liveLogs[run.summary.runId] : undefined}
            onClose={() => setInspectedNode(undefined)}
          />
        )}
      </main>
    </div>
  );
}
