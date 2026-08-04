import {
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { OperatorApi } from "./api";
import { Explorer, type Selection } from "./Explorer";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { RunControls } from "./RunControls";
import { RunListPanel } from "./RunListPanel";
import { useOperatorProjection } from "./state";

const EXPLORER_MIN_WIDTH = 220;
const EXPLORER_MAX_WIDTH = 420;
const EXPLORER_DEFAULT_WIDTH = 280;
const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 640;
const INSPECTOR_DEFAULT_WIDTH = 410;
const PANEL_KEYBOARD_STEP = 16;

interface WorkspaceDividerProps {
  className: string;
  label: string;
  controls: string;
  value: number;
  min: number;
  max: number;
  pointerDirection: 1 | -1;
  onChange: (value: number) => void;
}


function WorkspaceDivider({
  className,
  label,
  controls,
  value,
  min,
  max,
  pointerDirection,
  onChange,
}: WorkspaceDividerProps) {
  const dragStart = useRef<{ clientX: number; value: number } | undefined>(undefined);

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragStart.current) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragStart.current = undefined;
  };

  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    let next: number | undefined;
    if (event.key === "ArrowLeft") {
      next = value - PANEL_KEYBOARD_STEP * pointerDirection;
    }
    if (event.key === "ArrowRight") {
      next = value + PANEL_KEYBOARD_STEP * pointerDirection;
    }
    if (event.key === "Home") next = min;
    if (event.key === "End") next = max;
    if (next === undefined) return;
    event.preventDefault();
    onChange(Math.min(max, Math.max(min, next)));
  };

  return (
    <div
      className={`workspace-divider ${className}`}
      role="separator"
      aria-label={label}
      aria-controls={controls}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      aria-valuetext={`${value} pixels`}
      tabIndex={0}
      onKeyDown={resizeWithKeyboard}
      onPointerDown={(event) => {
        event.preventDefault();
        dragStart.current = { clientX: event.clientX, value };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        const start = dragStart.current;
        if (!start) return;
        const next =
          start.value + (event.clientX - start.clientX) * pointerDirection;
        onChange(Math.min(max, Math.max(min, next)));
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    />
  );
}

export function App({ api }: { api: OperatorApi }) {
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [selection, setSelection] = useState<Selection>();
  const [inspectedNode, setInspectedNode] = useState<string>();
  const [explorerOpen, setExplorerOpen] = useState(false);
  const [explorerCollapsed, setExplorerCollapsed] = useState(false);
  const [explorerWidth, setExplorerWidth] = useState(EXPLORER_DEFAULT_WIDTH);
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
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
  const run =
    historical &&
    state.selectedRunId === selection.runId &&
    state.selectedRunStatus === "ready" &&
    state.selectedRun?.summary?.runId === selection.runId
      ? state.selectedRun
      : undefined;
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const closeNode = useCallback(() => setInspectedNode(undefined), []);
  const collapseExplorer = useCallback(() => setExplorerCollapsed(true), []);
  const restoreExplorer = useCallback(() => setExplorerCollapsed(false), []);
  const select = useCallback(
    (next: Selection) => {
      setSelection(next);
      setInspectedNode(undefined);
      setExplorerOpen(false);
      void selectRun(next.kind === "run" ? next.runId : undefined);
    },
    [selectRun],
  );
  const selectWorkflowRun = useCallback(
    (runId: string) => {
      if (!workflow) return;
      select({ kind: "run", workflowId: workflow.workflowId, runId });
    },
    [select, workflow],
  );
  const restoreButton = explorerCollapsed ? (
    <button
      type="button"
      className="explorer-restore-button"
      aria-label="Restore Explorer"
      aria-controls="operator-explorer"
      aria-expanded="false"
      onClick={restoreExplorer}
    >
      <span aria-hidden="true">›</span>
    </button>
  ) : undefined;
  const runListPanel = workflow ? (
    <>
      {restoreButton}
      <RunListPanel
        workflowId={workflow.workflowId}
        runs={state.runs}
        selectedRunId={historical ? selection.runId : undefined}
        onSelectRun={selectWorkflowRun}
      />
    </>
  ) : undefined;
  const runStatus = run?.summary?.status;
  const showRunControls =
    Boolean(workflow) &&
    (!historical || runStatus === "pending" || runStatus === "running");
  const runControlsPanel = showRunControls ? (
    <RunControls
      workflow={!historical ? workflow : undefined}
      run={run}
      pending={state.action}
      onStart={startRun}
      onCancel={cancelRun}
    />
  ) : undefined;

  const liveDescriptorKey =
    historical && inspectedNode ? `${selection.runId}:${inspectedNode}` : "";
  const inspectorOpen = Boolean(inspectedNode && (!historical || run));
  const workspaceStyle = {
    "--workspace-explorer-width": `${explorerWidth}px`,
    "--workspace-inspector-width": `${inspectorWidth}px`,
  } as CSSProperties;

  return (
    <div
      className={`app-shell ${explorerOpen ? "explorer-open" : ""} ${
        explorerCollapsed ? "explorer-collapsed" : ""
      }`}
    >
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
      <main
        className={`workspace ${inspectorOpen ? "with-inspector" : ""}`}
        style={workspaceStyle}
      >
        <Explorer
          catalog={state.catalog}
          selection={selection}
          onSelect={select}
          onCollapse={collapseExplorer}
        />
        {!explorerCollapsed && (
          <WorkspaceDivider
            className="workspace-explorer-divider"
            label="Resize Explorer"
            controls="operator-explorer"
            value={explorerWidth}
            min={EXPLORER_MIN_WIDTH}
            max={EXPLORER_MAX_WIDTH}
            pointerDirection={1}
            onChange={setExplorerWidth}
          />
        )}
        <section className="canvas-shell">
          <div className={historical ? "canvas run-canvas" : "canvas blueprint-canvas"}>
            {historical ? (
              run ? (
                <>
                  <GraphCanvas
                    runTopology={run.topology}
                    runNodes={run.nodes}
                    onOpenNode={openNode}
                    topLeftPanel={runListPanel}
                    bottomRightPanel={runControlsPanel}
                  />
                  <div className="historical-badge">
                    <span>Immutable run snapshot</span>
                    Current workflow changes do not alter this canvas
                  </div>
                </>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "loading" ? (
                <>
                  {restoreButton}
                  <div className="empty-state" role="status">
                    <span>◇</span>
                    <h2>Loading run snapshot</h2>
                    <p>Retrieving the retained topology and execution state.</p>
                  </div>
                </>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "error" ? (
                <>
                  {restoreButton}
                  <div className="empty-state" role="alert">
                    <span>!</span>
                    <h2>Run snapshot unavailable</h2>
                    <p>{state.selectedRunError || "The selected run could not be loaded."}</p>
                  </div>
                </>
              ) : (
                <>
                  {restoreButton}
                  <div className="empty-state">
                    <span>◇</span>
                    <h2>No run snapshot</h2>
                    <p>Select the run again to load its retained topology.</p>
                  </div>
                </>
              )
            ) : workflow ? (
              <GraphCanvas
                workflow={workflow}
                topLeftPanel={runListPanel}
                bottomRightPanel={runControlsPanel}
                onOpenNode={openNode}
              />
            ) : (
              <>
                {restoreButton}
                <div className="empty-state">
                  <span>◇</span>
                  <h2>No workflows discovered</h2>
                  <p>Catalog changes will appear here as the operator scans configured targets.</p>
                </div>
              </>
            )}
          </div>
        </section>
        {inspectorOpen && (
          <>
            <WorkspaceDivider
              className="workspace-inspector-divider"
              label="Resize Inspector"
              controls="operator-inspector"
              value={inspectorWidth}
              min={INSPECTOR_MIN_WIDTH}
              max={INSPECTOR_MAX_WIDTH}
              pointerDirection={-1}
              onChange={setInspectorWidth}
            />
            <div id="operator-inspector" className="workspace-inspector-pane">
              <Inspector
                api={api}
                workflow={workflow}
                run={run}
                nodeId={inspectedNode}
                liveEvents={state.liveEvents[liveDescriptorKey]}
                liveLogs={historical ? state.liveLogs[liveDescriptorKey] : undefined}
                onClose={closeNode}
              />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
