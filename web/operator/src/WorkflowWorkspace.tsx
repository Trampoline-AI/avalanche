import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import type { OperatorApi } from "./api";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import type { FlowInfoMsg } from "./model";
import { RunLogPane } from "./RunLogPane";
import { RunControls } from "./RunControls";
import { compareNewestRun, RunListPanel } from "./RunListPanel";
import type { OperatorProjection } from "./state";
import { useOperatorProjection } from "./state";
import { WorkspaceDivider } from "./WorkspaceDivider";

const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 640;
const INSPECTOR_DEFAULT_WIDTH = 410;

type WorkspaceMode = "runs" | "dag";

export interface WorkflowWorkspaceProps {
  api: OperatorApi;
  workflowId: string;
  className?: string;
  onSelectedRunChange?: (runId: string | undefined) => void;
  runActionsEnabled?: boolean;
}

interface WorkflowWorkspaceSurfaceProps {
  api: OperatorApi;
  state: OperatorProjection;
  workflow?: FlowInfoMsg;
  selectedRunId?: string;
  onSelectRun: (runId: string | undefined) => void;
  selectRun: (runId?: string) => Promise<void>;
  startRun: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  cancelRun: (runId: string) => Promise<void>;
  leadingRunPanel?: ReactNode;
  workflowReloadDescription?: string;
  className?: string;
  runActionsEnabled?: boolean;
}

/** Both hosts share selection policy, tabs, graph, run controls, and the inspector column. */
export function WorkflowWorkspaceSurface({
  api,
  state,
  workflow,
  selectedRunId,
  onSelectRun,
  selectRun,
  startRun,
  cancelRun,
  leadingRunPanel,
  workflowReloadDescription = "Workflow change detected. Scanning...",
  className = "",
  runActionsEnabled = true,
}: WorkflowWorkspaceSurfaceProps) {
  const [mode, setMode] = useState<WorkspaceMode>("runs");
  const [inspectedNode, setInspectedNode] = useState<string>();
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
  const workspaceId = useId();
  const workflowId = workflow?.workflowId;
  const latestRunId = useMemo(() => {
    let newest: (typeof state.runs)[string] | undefined;
    for (const run of Object.values(state.runs)) {
      if (run.workflowId === workflowId && (!newest || compareNewestRun(run, newest) < 0)) {
        newest = run;
      }
    }
    return newest?.runId;
  }, [state.runs, workflowId]);
  // User intent is independent of snapshots: reconnect baselines can clear the projection,
  // and a burst of new runs must not turn a deliberate older selection into latest-following.
  const selection = useRef<{
    workflowId?: string;
    observedRunId?: string;
    following: boolean;
    pendingSelection?: { runId?: string };
  }>({ following: true });

  useEffect(() => {
    if (!workflowId || state.connection !== "live") return;
    const current = selection.current;
    if (current.workflowId !== workflowId) {
      current.workflowId = workflowId;
      current.observedRunId = selectedRunId;
      current.following = !selectedRunId || selectedRunId === latestRunId;
      current.pendingSelection = undefined;
      setMode("runs");
      setInspectedNode(undefined);
    } else if (current.observedRunId !== selectedRunId) {
      if (!current.pendingSelection || current.pendingSelection.runId !== selectedRunId) {
        // A host route change, rather than a selection made by this workspace.
        setMode(selectedRunId ? "runs" : "dag");
        current.following = !selectedRunId || selectedRunId === latestRunId;
        setInspectedNode(undefined);
      }
      current.observedRunId = selectedRunId;
      current.pendingSelection = undefined;
    }
    const desiredRunId = current.following ? latestRunId : selectedRunId;
    if (
      desiredRunId !== selectedRunId &&
      (!current.pendingSelection || current.pendingSelection.runId !== desiredRunId)
    ) {
      current.pendingSelection = { runId: desiredRunId };
      onSelectRun(desiredRunId);
    }
    // A fresh baseline resets projection selection; retain and reload the user's intent.
    if (state.selectedRunId !== desiredRunId) void selectRun(desiredRunId);
  }, [
    latestRunId,
    onSelectRun,
    selectRun,
    selectedRunId,
    state.connection,
    state.selectedRunId,
    workflowId,
  ]);

  useEffect(() => {
    setInspectedNode(undefined);
  }, [selectedRunId, mode]);

  const selectWorkflowRun = useCallback(
    (runId: string) => {
      selection.current.pendingSelection = { runId };
      selection.current.following = runId === latestRunId;
      onSelectRun(runId);
      void selectRun(runId);
      setMode("runs");
    },
    [latestRunId, onSelectRun, selectRun],
  );
  const closePanel = useCallback(() => setInspectedNode(undefined), []);
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const viewCurrentWorkflow = useCallback(() => {
    setMode("dag");
    setInspectedNode(undefined);
  }, []);
  const changeMode = (next: WorkspaceMode) => {
    setMode(next);
    setInspectedNode(undefined);
  };

  const historical = mode === "runs" && selectedRunId !== undefined;
  const run =
    historical &&
    state.selectedRunId === selectedRunId &&
    state.selectedRunStatus === "ready" &&
    state.selectedRun?.summary?.runId === selectedRunId
      ? state.selectedRun
      : undefined;
  const inspectorOpen = Boolean(inspectedNode && (!historical || run));
  const runListPanel =
    mode === "runs" && workflow ? (
      <RunListPanel
        workflowId={workflow.workflowId}
        runs={state.runs}
        selectedRunId={selectedRunId}
        onSelectRun={selectWorkflowRun}
      />
    ) : undefined;
  const runControlsPanel =
    workflow && (!historical || run) ? (
      <RunControls
        workflow={!historical ? workflow : undefined}
        run={run}
        pending={state.action}
        onStart={startRun}
        onCancel={cancelRun}
        onViewWorkflow={historical ? viewCurrentWorkflow : undefined}
        runActionsEnabled={runActionsEnabled}
      />
    ) : undefined;
  const liveEventDescriptorKey =
    historical && inspectedNode ? `${selectedRunId}:${inspectedNode}` : "";
  const workspaceStyle = {
    "--workspace-inspector-width": `${inspectorWidth}px`,
    "--workspace-inspector-column-width": inspectorOpen ? `${inspectorWidth}px` : "0px",
    "--workspace-inspector-divider-width": "0px",
  } as CSSProperties;

  return (
    <section
      className={`avalanche-operator-ui avalanche-workspace flex min-h-0 min-w-0 flex-col ${className}`}
      style={{ height: "100%" }}
    >
      <div className="flex min-h-11 shrink-0 items-center gap-3 border-b border-line bg-white px-3.5">
        {leadingRunPanel}
        <div role="tablist" aria-label="Workflow view" className="flex h-full gap-4">
          {(["runs", "dag"] as const).map((tab) => (
            <button
              key={tab}
              id={`${workspaceId}-${tab}`}
              type="button"
              role="tab"
              aria-selected={mode === tab}
              aria-controls={`${workspaceId}-content`}
              tabIndex={mode === tab ? 0 : -1}
              onClick={() => changeMode(tab)}
              onKeyDown={(event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const next =
                  event.key === "Home"
                    ? "runs"
                    : event.key === "End"
                      ? "dag"
                      : mode === "runs"
                        ? "dag"
                        : "runs";
                changeMode(next);
                document.getElementById(`${workspaceId}-${next}`)?.focus();
              }}
              className={`cursor-pointer border-0 border-b-2 bg-transparent px-1 py-3 text-xs font-medium focus-visible:outline-2 focus-visible:outline-acid ${mode === tab ? "border-acid text-ink" : "border-transparent text-secondary hover:text-ink"}`}
            >
              {tab === "runs" ? "Runs" : "DAG"}
            </button>
          ))}
        </div>
      </div>
      <div
        id={`${workspaceId}-content`}
        role="tabpanel"
        aria-labelledby={`${workspaceId}-${mode}`}
        className={`workflow-workspace relative grid min-h-0 min-w-0 w-full flex-1 overflow-hidden grid-cols-[minmax(0,1fr)_var(--workspace-inspector-divider-width)_var(--workspace-inspector-column-width)] max-[1000px]:grid-cols-[minmax(0,1fr)] ${inspectorOpen ? "with-inspector" : ""}`}
        style={workspaceStyle}
      >
        <section className="canvas-shell relative col-start-1 flex min-h-0 min-w-0 flex-col overflow-hidden bg-[#f7f9f8]">
          <div
            className={`canvas relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden ${historical ? "run-canvas bg-[#fafaf8]" : "blueprint-canvas bg-white"}`}
          >
            {workflow ? (
              <>
                <div className="run-graph-shell relative min-h-0 min-w-0 flex-1 overflow-hidden">
                  <GraphCanvas
                    workflow={run ? undefined : workflow}
                    runTopology={run?.topology}
                    runNodes={run?.nodes}
                    selectedNodeId={inspectedNode}
                    onClearNode={closePanel}
                    onOpenNode={openNode}
                    inspectionDisabled={mode === "runs" && !run}
                    topLeftPanel={runListPanel}
                    bottomRightPanel={runControlsPanel}
                  />
                  {historical && !run && (
                    <div
                      className="absolute right-3 top-3 max-w-[240px] rounded-lg border border-line bg-panel p-3 text-xs text-secondary"
                      role={state.selectedRunStatus === "error" ? "alert" : "status"}
                    >
                      {state.selectedRunStatus === "error" ? (
                        <>
                          <strong className="block text-ink">Run snapshot unavailable</strong>
                          {state.selectedRunError || "The selected run could not be loaded."}
                        </>
                      ) : (
                        "Loading run snapshot"
                      )}
                    </div>
                  )}
                  {run && (
                    <div className="historical-badge pointer-events-none absolute top-[18px] right-[18px] z-[5] rounded-lg border border-[#dfc99e] bg-[rgba(255,252,245,.96)] px-3 py-[9px] text-[9px] text-[#766548] shadow-[0_4px_14px_rgba(54,44,25,.08)] max-[700px]:hidden [&>span]:mb-[3px] [&>span]:block [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-amber [&>span]:uppercase">
                      <span>Immutable run snapshot</span>Current workflow changes do not alter
                      this canvas
                    </div>
                  )}
                </div>
                {run && (
                  <RunLogPane
                    api={api}
                    run={run}
                    nodeId={inspectedNode}
                    liveLogs={selectedRunId ? state.liveLogs[selectedRunId] : undefined}
                    onSelectNode={openNode}
                  />
                )}
              </>
            ) : (
              <div className="empty-state grid h-full place-content-center text-center text-secondary">
                <h2>No workflows discovered</h2>
                <p>
                  Catalog changes will appear here as the operator scans configured targets.
                </p>
              </div>
            )}
          </div>
        </section>
        {inspectorOpen && (
          <>
            <WorkspaceDivider
              className="workspace-inspector-divider col-start-2 z-[5] w-4 -translate-x-1/2 max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-[calc(var(--workspace-inspector-width)-8px)] max-[1000px]:translate-x-0 max-[1000px]:z-30 max-[700px]:hidden"
              label="Resize Inspector"
              controls={`${workspaceId}-inspector`}
              value={inspectorWidth}
              min={INSPECTOR_MIN_WIDTH}
              max={INSPECTOR_MAX_WIDTH}
              pointerDirection={-1}
              onChange={setInspectorWidth}
            />
            <div
              id={`${workspaceId}-inspector`}
              className="workspace-inspector-pane col-start-3 grid min-h-0 min-w-0 overflow-hidden bg-panel max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-0 max-[1000px]:z-30 max-[1000px]:w-[var(--workspace-inspector-width)] max-[1000px]:max-w-full"
            >
              <Inspector
                api={api}
                embedded
                workflow={workflow}
                run={run}
                nodeId={inspectedNode}
                liveEvents={state.liveEvents[liveEventDescriptorKey]}
                onClose={closePanel}
              />
            </div>
          </>
        )}
      </div>
      {state.workflowReloading && (
        <div
          className="workflow-reload-indicator pointer-events-none fixed inset-x-0 bottom-5 z-50 flex justify-center px-4"
          role="status"
          aria-live="polite"
        >
          <div className="flex items-center gap-2.5 rounded-lg border border-white/10 bg-[#1d2923] px-3.5 py-2.5 text-[11px] font-medium tracking-[-0.01em] text-white shadow-[0_10px_30px_rgba(15,25,20,.22)]">
            <span className="relative flex size-2" aria-hidden="true">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-acid opacity-60 motion-reduce:animate-none" />
              <span className="relative inline-flex size-2 rounded-full bg-acid" />
            </span>
            {workflowReloadDescription}
          </div>
        </div>
      )}
    </section>
  );
}

/** A complete workspace for a host-selected workflow, with no host-owned run navigation. */
export function WorkflowWorkspace({
  api,
  workflowId,
  className,
  onSelectedRunChange,
  runActionsEnabled = true,
}: WorkflowWorkspaceProps) {
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [selection, setSelection] = useState<{ workflowId: string; runId?: string }>();
  const selectedRunId = selection?.workflowId === workflowId ? selection.runId : undefined;
  const notifiedRunId = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (notifiedRunId.current === selectedRunId) return;
    notifiedRunId.current = selectedRunId;
    onSelectedRunChange?.(selectedRunId);
  }, [onSelectedRunChange, selectedRunId]);
  const onSelectRun = useCallback(
    (runId: string | undefined) => {
      setSelection({ workflowId, runId });
    },
    [workflowId],
  );
  const workflow = state.catalog?.workflows.find((item) => item.workflowId === workflowId);

  if (!state.catalog) {
    return (
      <section
        className={`avalanche-operator-ui avalanche-workspace grid min-h-[32rem] place-items-center bg-canvas p-6 text-center ${className ?? ""}`}
        style={{ height: "100%" }}
        role="status"
        aria-live="polite"
      >
        <div className="grid justify-items-center gap-4">
          <span className="size-7 animate-spin rounded-full border-2 border-acid border-t-transparent motion-reduce:animate-none" />
          <div>
            <h2 className="text-lg font-semibold tracking-[-0.02em] text-ink">
              Reconnecting...
            </h2>
            <p className="mt-2 text-sm text-muted">
              The workflow service is not available yet.
            </p>
          </div>
        </div>
      </section>
    );
  }
  return (
    <WorkflowWorkspaceSurface
      api={api}
      state={state}
      workflow={workflow}
      selectedRunId={selectedRunId}
      onSelectRun={onSelectRun}
      selectRun={selectRun}
      startRun={startRun}
      cancelRun={cancelRun}
      className={className}
      runActionsEnabled={runActionsEnabled}
    />
  );
}
