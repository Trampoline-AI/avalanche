import {
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { PanelLeftOpen } from "lucide-react";

import type { OperatorApi } from "./api";
import { Explorer, type Selection } from "./Explorer";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { RunLogPane } from "./RunLogPane";
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
      className={`workspace-divider relative z-[4] min-h-0 min-w-0 cursor-col-resize touch-none bg-panel after:absolute after:inset-y-0 after:left-1/2 after:w-0.5 after:-translate-x-1/2 after:bg-line after:content-[''] after:transition-colors after:duration-150 hover:after:bg-acid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid focus-visible:after:bg-acid ${className}`}
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
  const startWorkflowRun = useCallback(
    async (workflowSelector: string, input?: Record<string, unknown>) => {
      const runId = await startRun(workflowSelector, input);
      if (workflow) {
        select({ kind: "run", workflowId: workflow.workflowId, runId });
      }
      return runId;
    },
    [select, startRun, workflow],
  );
  const restoreButton = explorerCollapsed ? (
    <button
      type="button"
      className="explorer-restore-button grid size-7 flex-none cursor-pointer place-items-center rounded-[7px] border border-line bg-white p-0 text-secondary hover:border-secondary hover:bg-[#f7f9f8] hover:text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid max-[700px]:hidden"
      aria-label="Restore Explorer"
      aria-controls="operator-explorer"
      aria-expanded="false"
      onClick={restoreExplorer}
    >
      <PanelLeftOpen aria-hidden="true" className="size-4" strokeWidth={1.8} />
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
      onStart={startWorkflowRun}
      onCancel={cancelRun}
    />
  ) : undefined;

  const liveEventDescriptorKey =
    historical && inspectedNode ? `${selection.runId}:${inspectedNode}` : "";
  const inspectorOpen = Boolean(inspectedNode && (!historical || run));
  const workspaceStyle = {
    "--workspace-explorer-width": `${explorerWidth}px`,
    "--workspace-inspector-width": `${inspectorWidth}px`,
    "--workspace-explorer-column-width": explorerCollapsed ? "0px" : `${explorerWidth}px`,
    "--workspace-explorer-divider-width": "0px",
    "--workspace-inspector-column-width": inspectorOpen ? `${inspectorWidth}px` : "0px",
    "--workspace-inspector-divider-width": inspectorOpen ? "16px" : "0px",
  } as CSSProperties;

  return (
    <div
      className={`app-shell flex h-full flex-col ${explorerOpen ? "explorer-open" : ""} ${
        explorerCollapsed ? "explorer-collapsed" : ""
      }`}
    >
      <header className="topbar relative z-10 grid min-h-[58px] grid-cols-[260px_minmax(0,1fr)_auto_auto] items-center border-b border-line bg-white px-5 shadow-[0_1px_2px_rgba(20,31,26,.04)] max-[1000px]:grid-cols-[210px_minmax(0,1fr)_auto_auto] max-[700px]:grid-cols-[auto_minmax(0,1fr)_auto] max-[700px]:gap-2 max-[700px]:px-2.5">
        <div className="brand flex items-center gap-[11px]">
          <span className="brand-mark grid size-[30px] place-items-center rounded-lg bg-acid text-white [font-weight:750]" aria-hidden="true">
            A
          </span>
          <div className="flex items-baseline gap-[7px]">
            <strong className="text-[15px] tracking-[-0.02em]">Avalanche</strong>
            <span className="font-mono text-[11px] text-muted uppercase">Operator</span>
          </div>
        </div>
        <div className="breadcrumb absolute left-1/2 flex -translate-x-1/2 justify-center gap-[9px] text-xs text-muted max-[700px]:hidden [&_i]:opacity-40 [&_strong]:font-semibold [&_strong]:text-[#26322c]">
          <span>{workflow?.rootAlias || "Local operator"}</span>
          {workflow && <><i>/</i><strong>{workflow.displayName}</strong></>}
          {historical && <><i>/</i><strong>{selection.runId}</strong></>}
        </div>
        <div className={`connection flex items-center gap-2 font-mono text-[11px] capitalize [&>span]:size-[7px] [&>span]:rounded-full ${state.connection === "live" ? "[&>span]:bg-mint" : "[&>span]:bg-amber"} max-[700px]:justify-self-end connection-${state.connection}`}>
          <span />
          {state.connection === "live" ? "Live" : state.connection}
        </div>
        <button
          type="button"
          className="explorer-toggle hidden cursor-pointer rounded-[7px] border border-[#cbd2ce] bg-white px-[9px] py-[7px] text-[10px] max-[700px]:block"
          aria-controls="operator-explorer"
          aria-expanded={explorerOpen}
          onClick={() => setExplorerOpen((open) => !open)}
        >
          Explorer
        </button>
      </header>
      {state.error && <div className="connection-error border-b border-[#efb9b5] bg-[#fff1f0] px-[18px] py-2 text-xs text-[#9d2923]">{state.error}</div>}
      <main
        className={`workspace grid min-h-0 w-full flex-1 overflow-hidden grid-cols-[var(--workspace-explorer-column-width)_var(--workspace-explorer-divider-width)_minmax(0,1fr)_var(--workspace-inspector-divider-width)_var(--workspace-inspector-column-width)] max-[1000px]:grid-cols-[var(--workspace-explorer-column-width)_var(--workspace-explorer-divider-width)_minmax(0,1fr)] max-[700px]:grid-cols-[minmax(0,1fr)] ${inspectorOpen ? "with-inspector" : ""}`}
        style={workspaceStyle}
      >
        <Explorer
          catalog={state.catalog}
          selection={selection}
          onSelect={select}
          onCollapse={collapseExplorer}
          open={explorerOpen}
          collapsed={explorerCollapsed}
        />
        {!explorerCollapsed && (
          <WorkspaceDivider
            className="workspace-explorer-divider col-start-2 z-[5] w-4 -translate-x-1/2 max-[700px]:hidden"
            label="Resize Explorer"
            controls="operator-explorer"
            value={explorerWidth}
            min={EXPLORER_MIN_WIDTH}
            max={EXPLORER_MAX_WIDTH}
            pointerDirection={1}
            onChange={setExplorerWidth}
          />
        )}
        <section className="canvas-shell relative col-start-3 grid min-h-0 min-w-0 w-full grid-rows-[minmax(0,1fr)] overflow-hidden bg-[#f7f9f8] max-[700px]:col-start-1">
          <div className={historical ? "canvas run-canvas relative flex min-h-0 min-w-0 w-full flex-col overflow-hidden bg-[radial-gradient(circle,#e1e4df_1px,transparent_1px),#fafaf8] bg-[length:24px_24px]" : "canvas blueprint-canvas relative min-h-0 min-w-0 w-full overflow-hidden bg-white"}>
            {historical ? (
              run ? (
                <>
                  <div className="run-graph-shell relative min-h-0 min-w-0 flex-[1_1_auto] overflow-hidden">
                    <GraphCanvas
                      runTopology={run.topology}
                      runNodes={run.nodes}
                      selectedNodeId={inspectedNode}
                      onClearNode={closeNode}
                      onOpenNode={openNode}
                      topLeftPanel={runListPanel}
                      bottomRightPanel={runControlsPanel}
                    />
                    <div className="historical-badge absolute top-[18px] right-[18px] z-[5] rounded-lg border border-[#dfc99e] bg-[rgba(255,252,245,.96)] px-3 py-[9px] text-[9px] text-[#766548] shadow-[0_4px_14px_rgba(54,44,25,.08)] [&>span]:mb-[3px] [&>span]:block [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-amber [&>span]:uppercase">
                      <span>Immutable run snapshot</span>
                      Current workflow changes do not alter this canvas
                    </div>
                  </div>
                  <RunLogPane
                    api={api}
                    run={run}
                    nodeId={inspectedNode}
                    liveLogs={state.liveLogs[selection.runId]}
                    onSelectNode={openNode}
                  />
                </>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "loading" ? (
                <>
                  {restoreButton}
                  <div className="empty-state grid h-full place-content-center text-center text-[#6d7872] [&>span]:text-[40px] [&>span]:text-acid [&>h2]:my-2 [&>h2]:text-[#27332d] [&>p]:max-w-[390px] [&>p]:text-xs" role="status">
                    <span>◇</span>
                    <h2>Loading run snapshot</h2>
                    <p>Retrieving the retained topology and execution state.</p>
                  </div>
                </>
              ) : state.selectedRunId === selection.runId &&
                state.selectedRunStatus === "error" ? (
                <>
                  {restoreButton}
                  <div className="empty-state grid h-full place-content-center text-center text-[#6d7872] [&>span]:text-[40px] [&>span]:text-acid [&>h2]:my-2 [&>h2]:text-[#27332d] [&>p]:max-w-[390px] [&>p]:text-xs" role="alert">
                    <span>!</span>
                    <h2>Run snapshot unavailable</h2>
                    <p>{state.selectedRunError || "The selected run could not be loaded."}</p>
                  </div>
                </>
              ) : (
                <>
                  {restoreButton}
                  <div className="empty-state grid h-full place-content-center text-center text-[#6d7872] [&>span]:text-[40px] [&>span]:text-acid [&>h2]:my-2 [&>h2]:text-[#27332d] [&>p]:max-w-[390px] [&>p]:text-xs">
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
                selectedNodeId={inspectedNode}
                onClearNode={closeNode}
                onOpenNode={openNode}
              />
            ) : (
              <>
                {restoreButton}
                <div className="empty-state grid h-full place-content-center text-center text-[#6d7872] [&>span]:text-[40px] [&>span]:text-acid [&>h2]:my-2 [&>h2]:text-[#27332d] [&>p]:max-w-[390px] [&>p]:text-xs">
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
              className="workspace-inspector-divider col-start-4 max-[1000px]:fixed max-[1000px]:top-[58px] max-[1000px]:right-[min(var(--workspace-inspector-width),100vw)] max-[1000px]:bottom-0 max-[1000px]:z-[31] max-[1000px]:w-4 max-[700px]:hidden"
              label="Resize Inspector"
              controls="operator-inspector"
              value={inspectorWidth}
              min={INSPECTOR_MIN_WIDTH}
              max={INSPECTOR_MAX_WIDTH}
              pointerDirection={-1}
              onChange={setInspectorWidth}
            />
            <div id="operator-inspector" className="workspace-inspector-pane col-start-5 grid min-h-0 min-w-0 overflow-hidden max-[1000px]:contents">
              <Inspector
                api={api}
                workflow={workflow}
                run={run}
                nodeId={inspectedNode}
                liveEvents={state.liveEvents[liveEventDescriptorKey]}
                onClose={closeNode}
              />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
