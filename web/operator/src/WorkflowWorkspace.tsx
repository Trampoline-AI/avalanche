import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { OperatorApi } from "./api";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import type { FlowInfoMsg, RunSnapshotMsg } from "./model";
import { RunLogPane } from "./RunLogPane";
import { RunControls } from "./RunControls";
import { compareNewestRun, RunListPanel } from "./RunListPanel";
import type { OperatorProjection } from "./state";
import { useOperatorProjection } from "./state";
import { WorkspaceDivider } from "./WorkspaceDivider";

const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 640;
const INSPECTOR_DEFAULT_WIDTH = 410;

export interface WorkflowWorkspaceProps {
  api: OperatorApi;
  workflowId: string;
  className?: string;
  onSelectedRunChange?: (runId: string | undefined) => void;
  runActionsEnabled?: boolean;
  definitionLabel?: string;
  /** Overrides the graph actions panel; null hides it, undefined uses built-in controls. */
  bottomRightPanel?: ReactNode;
  navigation?: {
    selectedRunId?: string;
    onSelectRun: (runId: string | undefined) => void;
  };
}

interface WorkflowWorkspaceSurfaceProps {
  api: OperatorApi;
  state: OperatorProjection;
  workflowId?: string;
  workflow?: FlowInfoMsg;
  selectedRunId?: string;
  onSelectRun: (runId: string | undefined) => void;
  selectRun: (runId?: string) => Promise<void>;
  startRun: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  cancelRun: (runId: string) => Promise<void>;
  workflowReloadDescription?: string;
  className?: string;
  runActionsEnabled?: boolean;
  definitionLabel?: string;
  bottomRightPanel?: ReactNode;
}

/** Both hosts share selection policy, graph, run controls, and the inspector column. */
export function WorkflowWorkspaceSurface({
  api,
  state,
  workflowId,
  workflow,
  selectedRunId,
  onSelectRun,
  selectRun,
  startRun,
  cancelRun,
  workflowReloadDescription = "Workflow change detected. Scanning...",
  className = "",
  runActionsEnabled = true,
  definitionLabel,
  bottomRightPanel,
}: WorkflowWorkspaceSurfaceProps) {
  const [inspectedNode, setInspectedNode] = useState<string>();
  const [timelineExpanded, setTimelineExpanded] = useState(false);
  const timelineElement = useRef<HTMLDivElement>(null);
  const timelineBounds = useRef<DOMRect | undefined>(undefined);
  const timelineAnimation = useRef<Animation | undefined>(undefined);
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
  const workspaceId = useId();
  const navigationGeneration = useRef(0);
  // Invalidate pending start navigation even when the user leaves and returns to the
  // same selection. Layout cleanup also closes the scope before unmount completes.
  useLayoutEffect(
    () => () => {
      navigationGeneration.current += 1;
    },
    [workflowId, selectedRunId],
  );
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
  }>({ following: false });

  useEffect(() => {
    if (!workflowId) return;
    const current = selection.current;
    if (current.workflowId !== workflowId) {
      current.workflowId = workflowId;
      current.observedRunId = selectedRunId;
      current.following = selectedRunId !== undefined && selectedRunId === latestRunId;
      current.pendingSelection = undefined;
      setInspectedNode(undefined);
      setTimelineExpanded(false);
    } else if (current.observedRunId !== selectedRunId) {
      if (!current.pendingSelection || current.pendingSelection.runId !== selectedRunId) {
        // A host route change, rather than a selection made by this workspace.
        current.following = selectedRunId !== undefined && selectedRunId === latestRunId;
      }
      current.observedRunId = selectedRunId;
      current.pendingSelection = undefined;
    }
    if (state.connection !== "live") return;
    const desiredRunId =
      current.following && latestRunId !== undefined ? latestRunId : selectedRunId;
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

  const selectWorkflowRun = useCallback(
    (runId: string | undefined) => {
      navigationGeneration.current += 1;
      selection.current.pendingSelection = { runId };
      selection.current.following = runId !== undefined && runId === latestRunId;
      onSelectRun(runId);
      void selectRun(runId);
    },
    [latestRunId, onSelectRun, selectRun],
  );
  const startWorkflowRun = useCallback(
    async (workflowSelector: string, input?: Record<string, unknown>) => {
      const generation = navigationGeneration.current;
      const runId = await startRun(workflowSelector, input);
      if (navigationGeneration.current === generation) selectWorkflowRun(runId);
      return runId;
    },
    [selectWorkflowRun, startRun],
  );
  const closePanel = useCallback(() => setInspectedNode(undefined), []);
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const toggleTimeline = useCallback((expanded: boolean) => {
    timelineBounds.current = timelineElement.current?.getBoundingClientRect();
    timelineAnimation.current?.cancel();
    setTimelineExpanded(expanded);
  }, []);
  const openAllRuns = useCallback(() => toggleTimeline(true), [toggleTimeline]);
  const closeAllRuns = useCallback(() => toggleTimeline(false), [toggleTimeline]);

  useLayoutEffect(() => {
    const element = timelineElement.current;
    const previous = timelineBounds.current;
    timelineBounds.current = undefined;
    if (
      !element?.animate ||
      !previous ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const next = element.getBoundingClientRect();
    const animation = element.animate(
      [
        {
          width: `${previous.width}px`,
          height: `${previous.height}px`,
          transform: `translate(${previous.left - next.left}px, ${previous.top - next.top}px)`,
          overflow: "hidden",
        },
        {
          width: `${next.width}px`,
          height: `${next.height}px`,
          transform: "translate(0, 0)",
          overflow: "hidden",
        },
      ],
      { duration: 220, easing: "cubic-bezier(0.2, 0, 0, 1)" },
    );
    timelineAnimation.current = animation;
    return () => animation.cancel();
  }, [timelineExpanded]);

  const historical = selectedRunId !== undefined;
  const loadedRun =
    historical &&
    state.selectedRunId === selectedRunId &&
    state.selectedRunStatus === "ready" &&
    state.selectedRun?.summary?.runId === selectedRunId
      ? state.selectedRun
      : undefined;
  const [retainedRun, setRetainedRun] = useState<RunSnapshotMsg>();
  useEffect(() => {
    setRetainedRun((current) => {
      if (!historical) return undefined;
      return loadedRun ?? current;
    });
  }, [historical, loadedRun]);
  const run =
    loadedRun ??
    (historical && retainedRun?.summary?.workflowId === workflowId ? retainedRun : undefined);
  const topologyNodeIds = run?.topology?.nodeIds ?? workflow?.nodeIds;
  useEffect(() => {
    if (!topologyNodeIds) return;
    setInspectedNode((current) =>
      current && !topologyNodeIds.includes(current) ? undefined : current,
    );
  }, [topologyNodeIds]);
  const inspectedNodeAvailable =
    !inspectedNode || !topologyNodeIds || topologyNodeIds.includes(inspectedNode);
  const inspectorOpen = Boolean(
    inspectedNode &&
    inspectedNodeAvailable &&
    (!run ||
      (run.topology
        ? Object.hasOwn(run.topology.agentFieldSchemasJson, inspectedNode)
        : run.nodes.some((node) => node.nodeId === inspectedNode && node.trace))),
  );
  const runListPanel = workflowId ? (
    <RunListPanel
      key={workflowId}
      expanded={timelineExpanded}
      workflowId={workflowId}
      runs={state.runs}
      selectedRunId={selectedRunId}
      onSelectRun={selectWorkflowRun}
      onViewAll={openAllRuns}
      onClose={closeAllRuns}
    />
  ) : undefined;
  const runControlsPanel = (historical ? loadedRun : workflow) ? (
    <RunControls
      workflow={!historical ? workflow : undefined}
      run={loadedRun}
      pending={state.action}
      onStart={startWorkflowRun}
      onCancel={cancelRun}
      runActionsEnabled={runActionsEnabled}
    />
  ) : undefined;
  const displayedRunId = run?.summary?.runId;
  const liveEventDescriptorKey =
    displayedRunId && inspectedNode ? `${displayedRunId}:${inspectedNode}` : "";
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
      <div
        className={`workflow-workspace relative grid min-h-0 min-w-0 w-full flex-1 overflow-hidden grid-cols-[minmax(0,1fr)_var(--workspace-inspector-divider-width)_var(--workspace-inspector-column-width)] max-[1000px]:grid-cols-[minmax(0,1fr)] ${inspectorOpen ? "with-inspector" : ""}`}
        style={workspaceStyle}
      >
        <section className="canvas-shell relative col-start-1 flex min-h-0 min-w-0 flex-col overflow-hidden bg-[#f7f9f8]">
          <div
            className={`canvas relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden ${run ? "run-canvas bg-[#fafaf8]" : "blueprint-canvas bg-white"}`}
          >
            {workflow || run ? (
              <>
                <div className="run-graph-shell relative min-h-0 min-w-0 flex-1 overflow-hidden">
                  <GraphCanvas
                    workflow={run ? undefined : workflow}
                    runTopology={run?.topology}
                    runNodes={run?.nodes}
                    selectedNodeId={inspectedNode}
                    onClearNode={closePanel}
                    onOpenNode={openNode}
                    bottomRightPanel={
                      bottomRightPanel === undefined ? runControlsPanel : bottomRightPanel
                    }
                  />
                  {historical && !loadedRun && (
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
                      <span>Viewing a run snapshot</span>This view does not represent the
                      workflow&apos;s current state.
                    </div>
                  )}
                </div>
                {run && (
                  <RunLogPane
                    api={api}
                    run={run}
                    nodeId={inspectedNode}
                    liveLogs={displayedRunId ? state.liveLogs[displayedRunId] : undefined}
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
          {runListPanel && (
            <div
              ref={timelineElement}
              className={`workspace-timeline absolute z-20 ${timelineExpanded ? "inset-y-0 left-0 w-[410px] max-w-full" : "top-3.5 left-3.5 w-[300px] max-w-[calc(100vw-3rem)]"}`}
            >
              {runListPanel}
            </div>
          )}
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
              className="workspace-inspector-pane col-start-3 grid min-h-0 min-w-0 overflow-hidden bg-panel max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-0 max-[1000px]:z-30 max-[1000px]:w-[var(--workspace-inspector-width)] max-[1000px]:max-w-full max-[700px]:w-full"
            >
              <Inspector
                api={api}
                embedded
                workflow={workflow}
                run={run}
                nodeId={inspectedNode}
                liveEvents={state.liveEvents[liveEventDescriptorKey]}
                onClose={closePanel}
                definitionLabel={definitionLabel}
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

/** A complete workspace with local or host-controlled run selection. */
export function WorkflowWorkspace({
  api,
  workflowId,
  className,
  onSelectedRunChange,
  runActionsEnabled = true,
  definitionLabel,
  bottomRightPanel,
  navigation,
}: WorkflowWorkspaceProps) {
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [selection, setSelection] = useState<{ workflowId: string; runId?: string }>();
  const selectedRunId = navigation
    ? navigation.selectedRunId
    : selection?.workflowId === workflowId
      ? selection.runId
      : undefined;
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
      workflowId={workflowId}
      workflow={workflow}
      selectedRunId={selectedRunId}
      onSelectRun={navigation ? navigation.onSelectRun : onSelectRun}
      selectRun={selectRun}
      startRun={startRun}
      cancelRun={cancelRun}
      className={className}
      runActionsEnabled={runActionsEnabled}
      definitionLabel={definitionLabel}
      bottomRightPanel={bottomRightPanel}
    />
  );
}
