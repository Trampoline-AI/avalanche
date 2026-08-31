import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { OperatorApi } from "./api";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import type { FlowInfoMsg } from "./model";
import { RunLogPane } from "./RunLogPane";
import { RunControls } from "./RunControls";
import { RunListPanel } from "./RunListPanel";
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
}

interface WorkflowWorkspaceSurfaceProps {
  api: OperatorApi;
  state: OperatorProjection;
  workflow?: FlowInfoMsg;
  selectedRunId?: string;
  onSelectRun: (runId: string | undefined) => void;
  startRun: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  cancelRun: (runId: string) => Promise<void>;
  leadingRunPanel?: ReactNode;
  className?: string;
  runActionsEnabled?: boolean;
}

function EmptyWorkspace({
  children,
  role,
}: {
  children: ReactNode;
  role?: "alert" | "status";
}) {
  return (
    <div
      className="empty-state grid h-full place-content-center text-center text-[#6d7872] [&>span]:text-[40px] [&>span]:text-acid [&>h2]:my-2 [&>h2]:text-[#27332d] [&>p]:max-w-[390px] [&>p]:text-xs"
      role={role}
    >
      {children}
    </div>
  );
}

/**
 * The graph, run list, run controls, logs, and inspector shared by local and hosted consoles.
 *
 * The host owns navigation and provides an OperatorApi. This surface owns only temporary node
 * inspection state and the inspector width.
 */
export function WorkflowWorkspaceSurface({
  api,
  state,
  workflow,
  selectedRunId,
  onSelectRun,
  startRun,
  cancelRun,
  leadingRunPanel,
  className = "",
  runActionsEnabled = true,
}: WorkflowWorkspaceSurfaceProps) {
  const [inspectedNode, setInspectedNode] = useState<string>();
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
  const previousSelection = useRef<{ runId?: string; workflowId?: string }>({});

  useEffect(() => {
    const previous = previousSelection.current;
    if (previous.runId !== selectedRunId || previous.workflowId !== workflow?.workflowId) {
      previousSelection.current = { runId: selectedRunId, workflowId: workflow?.workflowId };
      setInspectedNode(undefined);
    }
  }, [selectedRunId, workflow?.workflowId]);

  const historical = selectedRunId !== undefined;
  const run =
    historical &&
    state.selectedRunId === selectedRunId &&
    state.selectedRunStatus === "ready" &&
    state.selectedRun?.summary?.runId === selectedRunId
      ? state.selectedRun
      : undefined;
  const openNode = useCallback((nodeId: string) => setInspectedNode(nodeId), []);
  const closeNode = useCallback(() => setInspectedNode(undefined), []);
  const viewCurrentWorkflow = useCallback(() => onSelectRun(undefined), [onSelectRun]);
  const selectWorkflowRun = useCallback((runId: string) => onSelectRun(runId), [onSelectRun]);

  const runListPanel = workflow ? (
    <>
      {leadingRunPanel}
      <RunListPanel
        workflowId={workflow.workflowId}
        runs={state.runs}
        selectedRunId={historical ? selectedRunId : undefined}
        onSelectRun={selectWorkflowRun}
      />
    </>
  ) : undefined;
  const showRunControls = Boolean(workflow) && (!historical || Boolean(run));
  const runControlsPanel = showRunControls ? (
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
  const inspectorOpen = Boolean(inspectedNode && (!historical || run));
  const workspaceStyle = {
    "--workspace-inspector-width": `${inspectorWidth}px`,
    "--workspace-inspector-column-width": inspectorOpen ? `${inspectorWidth}px` : "0px",
    "--workspace-inspector-divider-width": inspectorOpen ? "16px" : "0px",
  } as CSSProperties;

  return (
    <section
      className={`avalanche-console avalanche-workspace min-h-0 min-w-0 ${className}`}
      style={{ height: "100%" }}
    >
      <div
        className={`workflow-workspace relative grid h-full min-h-0 min-w-0 w-full overflow-hidden grid-cols-[minmax(0,1fr)_var(--workspace-inspector-divider-width)_var(--workspace-inspector-column-width)] max-[1000px]:grid-cols-[minmax(0,1fr)] max-[700px]:grid-cols-[minmax(0,1fr)] ${
          inspectorOpen ? "with-inspector" : ""
        }`}
        style={workspaceStyle}
      >
        <section className="canvas-shell relative col-start-1 grid min-h-0 min-w-0 w-full grid-rows-[minmax(0,1fr)] overflow-hidden bg-[#f7f9f8]">
          <div
            className={
              historical
                ? "canvas run-canvas relative flex min-h-0 min-w-0 w-full flex-col overflow-hidden bg-[radial-gradient(circle,#e1e4df_1px,transparent_1px),#fafaf8] bg-[length:24px_24px]"
                : "canvas blueprint-canvas relative min-h-0 min-w-0 w-full overflow-hidden bg-white"
            }
          >
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
                    liveLogs={selectedRunId ? state.liveLogs[selectedRunId] : undefined}
                    onSelectNode={openNode}
                  />
                </>
              ) : state.selectedRunId === selectedRunId &&
                state.selectedRunStatus === "loading" ? (
                <>
                  {leadingRunPanel}
                  <EmptyWorkspace role="status">
                    <span>◇</span>
                    <h2>Loading run snapshot</h2>
                    <p>Retrieving the retained topology and execution state.</p>
                  </EmptyWorkspace>
                </>
              ) : state.selectedRunId === selectedRunId &&
                state.selectedRunStatus === "error" ? (
                <>
                  {leadingRunPanel}
                  <EmptyWorkspace role="alert">
                    <span>!</span>
                    <h2>Run snapshot unavailable</h2>
                    <p>{state.selectedRunError || "The selected run could not be loaded."}</p>
                  </EmptyWorkspace>
                </>
              ) : (
                <>
                  {leadingRunPanel}
                  <EmptyWorkspace>
                    <span>◇</span>
                    <h2>No run snapshot</h2>
                    <p>Select the run again to load its retained topology.</p>
                  </EmptyWorkspace>
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
                {leadingRunPanel}
                <EmptyWorkspace>
                  <span>◇</span>
                  <h2>No workflows discovered</h2>
                  <p>
                    Catalog changes will appear here as the operator scans configured targets.
                  </p>
                </EmptyWorkspace>
              </>
            )}
          </div>
        </section>
        {inspectorOpen && (
          <>
            <WorkspaceDivider
              className="workspace-inspector-divider col-start-2 z-[5] w-4 -translate-x-1/2 max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-[calc(var(--workspace-inspector-width)-8px)] max-[1000px]:z-30 max-[700px]:hidden"
              label="Resize Inspector"
              controls="operator-inspector"
              value={inspectorWidth}
              min={INSPECTOR_MIN_WIDTH}
              max={INSPECTOR_MAX_WIDTH}
              pointerDirection={-1}
              onChange={setInspectorWidth}
            />
            <div
              id="operator-inspector"
              className="workspace-inspector-pane col-start-3 grid min-h-0 min-w-0 overflow-hidden bg-panel max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-0 max-[1000px]:z-30 max-[1000px]:w-[var(--workspace-inspector-width)] max-[700px]:hidden"
            >
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
            Workflow change detected. Scanning...
          </div>
        </div>
      )}
    </section>
  );
}

/**
 * A complete workflow workspace for a host-selected workflow. The host owns route and page
 * chrome; this component owns run selection within that workflow.
 */
export function WorkflowWorkspace({
  api,
  workflowId,
  className,
  onSelectedRunChange,
  runActionsEnabled = true,
}: WorkflowWorkspaceProps) {
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const previousSelectedRunId = useRef(state.selectedRunId);

  const selectWorkspaceRun = useCallback(
    (runId: string | undefined) => {
      setSelectedRunId(runId);
      onSelectedRunChange?.(runId);
      void selectRun(runId);
    },
    [onSelectedRunChange, selectRun],
  );

  useEffect(() => {
    setSelectedRunId(undefined);
    void selectRun(undefined);
  }, [selectRun, workflowId]);

  useEffect(() => {
    const priorSelectedRunId = previousSelectedRunId.current;
    previousSelectedRunId.current = state.selectedRunId;
    if (
      selectedRunId === undefined ||
      state.selectedRunId !== undefined ||
      state.selectedRunStatus !== "idle" ||
      priorSelectedRunId !== selectedRunId
    ) {
      return;
    }

    if (state.runs[selectedRunId]) {
      void selectRun(selectedRunId);
      return;
    }

    selectWorkspaceRun(undefined);
  }, [
    selectWorkspaceRun,
    selectRun,
    selectedRunId,
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

  const workflow = state.catalog?.workflows.find((item) => item.workflowId === workflowId);
  if (state.connection !== "live") {
    return (
      <section
        className={`avalanche-console avalanche-workspace grid min-h-[32rem] place-items-center bg-canvas p-6 text-center ${className ?? ""}`}
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
      onSelectRun={selectWorkspaceRun}
      startRun={startRun}
      cancelRun={cancelRun}
      className={className}
      runActionsEnabled={runActionsEnabled}
    />
  );
}
