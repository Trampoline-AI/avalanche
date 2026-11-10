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
import {
  MachineRunActions,
  MachineRunWaits,
  MachineSendPanel,
  MachineVisitTimeline,
  MachineVisitWaits,
  machineVisits,
  useMachineHistory,
  useMachineInstance,
  useMachineVisit,
} from "./MachineWorkspace";
import type { MachineGraphView } from "./machineGraph";
import { GraphCanvas, type CanvasReserve } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import type { FlowInfoMsg, InputFileAttachment, RunSnapshotMsg } from "./model";
import { RunLogPane } from "./RunLogPane";
import { RunControls } from "./RunControls";
import { RunWaitPanel } from "./RunWaitPanel";
import { useInputDraft } from "./useInputDraft";
import { compareNewestRun, RunListPanel } from "./RunListPanel";
import type { OperatorProjection } from "./state";
import { useOperatorProjection } from "./state";
import { WorkspaceDivider } from "./WorkspaceDivider";
import { useRunEvaluations } from "./useRunEvaluations";
import { INPUT_NODE_ID } from "./workflowInput";

import {
  INSPECTOR_MIN_WIDTH,
  maximumInspectorWidth,
  useWorkspaceLayout,
} from "./useWorkspaceLayout";

/** A host-supplied deployment, ordered newest first within its workflow. */
export interface WorkspaceDeployment {
  /** Full deployment identifier, exposed in the row title and accessible name. */
  id: string;
  /** Visible row label; hosts may abbreviate the identifier here. */
  label: string;
  /** Creation time in epoch seconds, matching run timestamps. */
  createdAt: number;
  /** Marks the current deployment; at most one item should be current. */
  current: boolean;
}

/** Host-owned deployment data and actions; operator-ui renders the timeline. */
export interface WorkspaceDeployments {
  /** Newest-first deployments, already filtered to this workflow. */
  items: WorkspaceDeployment[];
  /** Whether another page is available. */
  hasMore: boolean;
  /** A page request is in progress; renders a loading row and blocks more requests. */
  loading: boolean;
  /** Requests a page near the bottom. Update loading/items when the request settles. */
  loadMore?: () => void;
  /** Inline loading error. Omitted when there is no error. */
  error?: string;
  /** Identifier to highlight; omitted when no deployment is selected. */
  selectedId?: string;
  /** Selects a deployment's definition; does not change the current deployment. */
  onSelect: (id: string) => void;
  /** Optional non-current-row action with a page-level confirmation modal. Absent: no action. */
  setCurrent?: {
    /** Action label, rendered lowercase. Default "set current". */
    label?: string;
    /** Modal title. Default "Set current deployment?". */
    confirmTitle?: string;
    /** Confirmation wording for the chosen deployment. */
    confirmText: (item: WorkspaceDeployment) => string;
    /** Moves the current pointer. Rejected Error messages are shown inside the modal. */
    onConfirm: (id: string) => Promise<void>;
  };
}

export interface WorkflowWorkspaceProps {
  /** Global panel-layout storage key. Default "avalanche-operator-ui.layout"; set to namespace a host. */
  layoutStorageKey?: string;
  /** Pinned Current row label. Default "Current definition". */
  currentLabel?: string;
  /** Muted trailing text action. Omitted: no action and no caret. */
  currentRowAction?: { label: string; onClick: () => void };
  /** Render the pinned current row on the Overview tab. Default true; false hides it on every tab. */
  showCurrentRow?: boolean;
  /** Enables Overview | Deployments tabs; omitted: the stock runs-only timeline. */
  deployments?: WorkspaceDeployments;
  /** Controlled timeline tab when set; otherwise internal state starts at "runs". */
  timelineView?: "runs" | "deployments";
  /** Observes tab requests in either mode; controlled hosts must update timelineView. */
  onTimelineViewChange?: (view: "runs" | "deployments") => void;
  api: OperatorApi;
  workflowId: string;
  className?: string;
  onSelectedRunChange?: (runId: string | undefined) => void;
  runActionsEnabled?: boolean;
  definitionLabel?: string;
  /** Overrides the graph actions panel; null hides it, undefined uses built-in controls. */
  bottomRightPanel?: ReactNode;
  /** Host loading surface used until the initial catalog is available. */
  loadingFallback?: ReactNode;
  /** Optional bottom pane when showing a definition rather than a run. */
  emptyRunPanel?: ReactNode;
  navigation?: {
    selectedRunId?: string;
    onSelectRun: (runId: string | undefined) => void;
  };
}

interface WorkflowWorkspaceSurfaceProps extends Pick<
  WorkflowWorkspaceProps,
  | "layoutStorageKey"
  | "loadingFallback"
  | "emptyRunPanel"
  | "currentLabel"
  | "currentRowAction"
  | "showCurrentRow"
  | "deployments"
  | "timelineView"
  | "onTimelineViewChange"
> {
  api: OperatorApi;
  state: OperatorProjection;
  workflowId?: string;
  workflow?: FlowInfoMsg;
  selectedRunId?: string;
  onSelectRun: (runId: string | undefined) => void;
  selectRun: (runId?: string) => Promise<void>;
  startRun: (
    workflowSelector: string,
    input?: Record<string, unknown>,
    inputFiles?: InputFileAttachment[],
  ) => Promise<string>;
  cancelRun: (runId: string) => Promise<void>;
  setScheduleEnabled: (
    workflowSelector: string,
    enabled: boolean,
    input?: Record<string, unknown>,
    inputFiles?: InputFileAttachment[],
  ) => Promise<void>;
  workflowReloadDescription?: string;
  className?: string;
  runActionsEnabled?: boolean;
  definitionLabel?: string;
  bottomRightPanel?: ReactNode;
}

/** Both hosts share selection policy, graph, run controls, and the inspector column. */
/** Room left under the floating timeline for the canvas controls (zoom, fit, orientation). */
const TIMELINE_CONTROLS_CLEARANCE = 210;
const TIMELINE_MIN_HEIGHT = 160;

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
  setScheduleEnabled,
  workflowReloadDescription = "Workflow change detected. Scanning...",
  className = "",
  runActionsEnabled = true,
  definitionLabel,
  bottomRightPanel,
  loadingFallback,
  emptyRunPanel,
  currentLabel,
  currentRowAction,
  showCurrentRow,
  deployments,
  timelineView,
  onTimelineViewChange,
  layoutStorageKey,
}: WorkflowWorkspaceSurfaceProps) {
  const [inspectedNode, setInspectedNode] = useState<string>();
  const { layout, setInspectorWidth, setTimelineExpanded, setLogHeight, setLogExpanded } =
    useWorkspaceLayout(layoutStorageKey);
  const { timelineExpanded, inspectorWidth } = layout;
  const timelineElement = useRef<HTMLDivElement>(null);
  const timelineBounds = useRef<DOMRect | undefined>(undefined);
  const timelineAnimation = useRef<Animation | undefined>(undefined);
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
    latestRunId?: string;
    awaitingStartedRunId?: string;
    following: boolean;
    pendingSelection?: { runId?: string };
  }>({ following: false });

  useEffect(() => {
    if (!workflowId) return;
    const current = selection.current;
    if (current.workflowId !== workflowId) {
      current.workflowId = workflowId;
      current.observedRunId = selectedRunId;
      current.latestRunId = latestRunId;
      current.awaitingStartedRunId = undefined;
      current.following = selectedRunId !== undefined && selectedRunId === latestRunId;
      current.pendingSelection = undefined;
      setInspectedNode(undefined);
    } else if (current.observedRunId !== selectedRunId) {
      if (!current.pendingSelection || current.pendingSelection.runId !== selectedRunId) {
        // A host route change, rather than a selection made by this workspace.
        current.following = selectedRunId !== undefined && selectedRunId === latestRunId;
        current.awaitingStartedRunId = undefined;
      }
      current.observedRunId = selectedRunId;
      current.pendingSelection = undefined;
    }
    if (state.connection !== "live") return;
    // Current follows arrivals, not the history already present when it was opened.
    if (
      selectedRunId === undefined &&
      timelineView !== "deployments" &&
      !current.pendingSelection &&
      latestRunId !== undefined &&
      latestRunId !== current.latestRunId
    ) {
      current.following = true;
    }
    current.latestRunId = latestRunId;
    // The start response can precede its creation event. Do not jump back to the
    // previous latest run while waiting for the started run to enter the timeline.
    if (current.awaitingStartedRunId && state.runs[current.awaitingStartedRunId]) {
      current.following = true;
      current.awaitingStartedRunId = undefined;
    }
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
    state.runs,
    state.selectedRunId,
    timelineView,
    workflowId,
  ]);

  const selectWorkflowRun = useCallback(
    (runId: string | undefined, started = false) => {
      navigationGeneration.current += 1;
      selection.current.pendingSelection =
        selection.current.observedRunId === runId ? undefined : { runId };
      selection.current.following = runId !== undefined && runId === latestRunId;
      selection.current.awaitingStartedRunId = started ? runId : undefined;
      onSelectRun(runId);
      void selectRun(runId);
    },
    [latestRunId, onSelectRun, selectRun],
  );
  const startWorkflowRun = useCallback(
    async (
      workflowSelector: string,
      input?: Record<string, unknown>,
      inputFiles?: InputFileAttachment[],
    ) => {
      const generation = navigationGeneration.current;
      const runId = await startRun(workflowSelector, input, inputFiles);
      if (navigationGeneration.current === generation) selectWorkflowRun(runId, true);
      return runId;
    },
    [selectWorkflowRun, startRun],
  );
  const closePanel = useCallback(() => setInspectedNode(undefined), []);
  // Keeps the inspected node while leaving the run for the current definition.
  const openCurrentDefinition = useCallback(
    () => selectWorkflowRun(undefined),
    [selectWorkflowRun],
  );
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

  // The collapsed timeline floats over the canvas's left edge; the machine graph is
  // fitted clear of its width (expanded, it is a column beside the canvas).
  const [timelineReserve, setTimelineReserve] = useState<CanvasReserve>();
  // The floating timeline stops above the canvas controls (bottom left) and scrolls.
  const graphShellElement = useRef<HTMLDivElement>(null);
  const [timelineMaxHeight, setTimelineMaxHeight] = useState<number>();
  useEffect(() => {
    const element = timelineElement.current;
    const shell = graphShellElement.current;
    if (!element || typeof ResizeObserver === "undefined") {
      setTimelineReserve(undefined);
      setTimelineMaxHeight(undefined);
      return;
    }
    const update = () => {
      const floating = getComputedStyle(element).position === "absolute" && !timelineExpanded;
      const next = floating
        ? { width: Math.round(element.offsetLeft + element.offsetWidth) }
        : undefined;
      setTimelineReserve((current) => (current?.width === next?.width ? current : next));
      const limit =
        floating && shell
          ? Math.max(TIMELINE_MIN_HEIGHT, shell.clientHeight - TIMELINE_CONTROLS_CLEARANCE)
          : undefined;
      setTimelineMaxHeight(limit);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    if (shell) observer.observe(shell);
    return () => observer.disconnect();
  }, [timelineExpanded, workflowId]);

  const historical = selectedRunId !== undefined;
  // One input draft for Run, the schedule and the graph's input popup.
  const inputDraft = useInputDraft(!historical ? workflow : undefined, api);
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
  const evaluationState = useRunEvaluations(
    api,
    run?.operatorInstanceId ?? "",
    run?.summary?.runId,
  );
  // A machine instance is a run: the same page, with the graph in machine mode,
  // its visits as a section of the run timeline and its detail in the side panel.
  const machineSpec = workflow?.machineSpec;
  const machineRunId = machineSpec ? selectedRunId : undefined;
  const machineRead = useMachineInstance(api, machineRunId);
  const machineInstance =
    machineRunId !== undefined && machineRead.value?.run_id === machineRunId
      ? machineRead.value
      : undefined;
  const machineHistory = useMachineHistory(api, machineRunId);
  const [visitSelection, setVisitSelection] = useState<{ runId: string; visit: number }>();
  const selectedVisit =
    visitSelection && visitSelection.runId === machineRunId ? visitSelection.visit : undefined;
  const currentVisit =
    selectedVisit !== undefined &&
    selectedVisit === machineInstance?.visit &&
    !machineInstance.finished;
  const visitRead = useMachineVisit(
    api,
    machineRunId,
    selectedVisit,
    currentVisit,
    `${machineInstance?.visit}:${machineInstance?.status}`,
  );
  const visit =
    selectedVisit !== undefined && visitRead.value?.visit === selectedVisit
      ? visitRead.value
      : undefined;
  // A selected visit's steps are shown exactly like a flow run's.
  const visitRun = visit?.snapshot;
  // Retention removed this visit's steps; say so instead of drawing another graph.
  const visitExpired = visit?.disposition === "expired";
  // The key-to-run link outlives history: the run stays, its history reads `expired`.
  const runExpired =
    run?.summary?.expired === true ||
    (selectedRunId !== undefined && state.runs[selectedRunId]?.expired === true);
  const [sendFor, setSendFor] = useState<string>();
  const sendOpen = machineRunId !== undefined && sendFor === machineRunId;
  const openSend = useCallback(() => {
    setInspectedNode(undefined);
    setSendFor(machineRunId);
  }, [machineRunId]);
  const selectVisit = useCallback(
    (next: number | undefined) => {
      setInspectedNode(undefined);
      setVisitSelection(
        next === undefined || machineRunId === undefined
          ? undefined
          : { runId: machineRunId, visit: next },
      );
    },
    [machineRunId],
  );
  const instanceSpecJson = machineInstance?.machine_spec
    ? JSON.stringify(machineInstance.machine_spec)
    : undefined;
  const shownMachineSpec = useMemo(
    () =>
      instanceSpecJson ? (JSON.parse(instanceSpecJson) as typeof machineSpec) : machineSpec,
    [instanceSpecJson, machineSpec],
  );
  const historyPage = machineHistory.value;
  const openMachineState = useCallback(
    (state: string) => {
      if (!machineInstance) return;
      const latest = machineVisits(machineInstance, historyPage).find(
        (item) => item.state === state,
      );
      if (latest) selectVisit(latest.visit);
    },
    [historyPage, machineInstance, selectVisit],
  );
  const machineState = machineInstance?.state;
  const machineStatus = machineInstance?.status;
  const machineDue = machineInstance?.due;
  const machineView = useMemo<MachineGraphView | undefined>(
    () =>
      shownMachineSpec
        ? {
            spec: shownMachineSpec,
            state: machineState,
            status: machineStatus,
            due: machineDue,
            onOpenState: machineRunId ? openMachineState : undefined,
          }
        : undefined,
    [machineDue, machineRunId, machineState, machineStatus, openMachineState, shownMachineSpec],
  );
  const requestingRun =
    run?.summary?.status === "requesting" ||
    (run !== undefined &&
      run === loadedRun &&
      state.selectedRunStructureRepairEventUlid !== undefined);
  // Requesting snapshots have no run-pinned graph yet. Keep the definition visible
  // through the status update and the subsequent prepared-snapshot refresh.
  const previewWorkflow = requestingRun && !run?.topology?.nodeIds.length;
  const shownTopology = visitRun
    ? visitRun.topology
    : previewWorkflow
      ? workflow
      : run
        ? run.topology
        : historical
          ? undefined
          : workflow;
  const shownNodeIds = shownTopology?.nodeIds;
  const shownInputDeclaration = shownTopology?.inputDeclarationJson;
  const topologyNodeIds = useMemo(
    () =>
      shownNodeIds && shownInputDeclaration ? [INPUT_NODE_ID, ...shownNodeIds] : shownNodeIds,
    [shownInputDeclaration, shownNodeIds],
  );
  useEffect(() => {
    if (!topologyNodeIds) return;
    setInspectedNode((current) =>
      current && !topologyNodeIds.includes(current) ? undefined : current,
    );
  }, [topologyNodeIds]);
  const inspectedNodeAvailable =
    !inspectedNode || !topologyNodeIds || topologyNodeIds.includes(inspectedNode);
  const nodeInspectorOpen = Boolean(
    inspectedNode && inspectedNodeAvailable && !requestingRun && (!historical || run),
  );
  // A machine run's send form opens in the side panel, like the run input form.
  const inspectorOpen = nodeInspectorOpen || (sendOpen && run !== undefined);
  const displayedRunId = run?.summary?.runId;
  // A live run is still changing; only a settled run is a snapshot of the past.
  const runStatus = run?.summary?.status;
  const runLive =
    runStatus === "requesting" ||
    runStatus === "pending" ||
    runStatus === "running" ||
    runStatus === "waiting" ||
    runStatus === "interrupted" ||
    runStatus === "needs_attention";
  // The run's waits sit beside the graph, never over it: a flow run's pending waits
  // (decision cards), or a machine run's timer, deferred events and receipts.
  const waitsPanel =
    machineRunId && run ? (
      <div aria-label="Machine run waits">
        {selectedVisit !== undefined && (
          <nav
            aria-label="Machine visit"
            className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 font-mono text-[10px] text-secondary"
          >
            <button
              type="button"
              className="cursor-pointer border-0 bg-transparent p-0 text-acid underline underline-offset-2"
              onClick={() => selectVisit(undefined)}
            >
              State machine
            </button>
            <span aria-hidden="true">/</span>
            <span className="text-ink">
              Visit {selectedVisit}
              {visit ? ` · ${visit.state}` : ""}
              {visitExpired ? " · expired" : ""}
            </span>
            {!visit && <span role="status">Loading visit…</span>}
          </nav>
        )}
        {selectedVisit !== undefined ? (
          <MachineVisitWaits
            api={api}
            runId={machineRunId}
            visit={visit}
            readError={visitRead.error}
            refresh={visitRead.refresh}
            current={currentVisit}
            enabled={runActionsEnabled && bottomRightPanel !== null}
          />
        ) : (
          <MachineRunWaits
            instance={machineInstance}
            readError={machineRead.error}
            refresh={machineRead.refresh}
          />
        )}
      </div>
    ) : run && displayedRunId && runActionsEnabled && runStatus === "running" ? (
      <RunWaitPanel key={displayedRunId} api={api} runId={displayedRunId} />
    ) : undefined;
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
      currentLabel={currentLabel}
      currentRowAction={currentRowAction}
      showCurrentRow={showCurrentRow}
      deployments={deployments}
      timelineView={timelineView}
      onTimelineViewChange={onTimelineViewChange}
      selectedRunSection={
        machineRunId || waitsPanel ? (
          <>
            {machineRunId && (
              <MachineVisitTimeline
                instance={machineInstance}
                history={machineHistory}
                selectedVisit={selectedVisit}
                onSelectVisit={selectVisit}
                expanded={timelineExpanded}
              />
            )}
            {waitsPanel && (
              // The run's waits are a section of its timeline, like visits: a flow
              // run's decision cards, a machine run's timer, events and receipts.
              <section
                aria-label="Run waits section"
                className="run-waits-section shrink-0 border-t border-run-list-line has-[>div:empty]:hidden"
              >
                <div className="flex h-7 items-center justify-between pl-[16px] pr-[30px] font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase">
                  <span>Waits</span>
                </div>
                <div
                  className={`overflow-auto overscroll-contain ${timelineExpanded ? "max-h-96" : "max-h-48"}`}
                >
                  {waitsPanel}
                </div>
              </section>
            )}
          </>
        ) : undefined
      }
    />
  ) : undefined;
  const normalRunControls = (historical ? loadedRun : workflow) ? (
    <RunControls
      workflow={!historical ? workflow : undefined}
      run={loadedRun}
      pending={state.action}
      onStart={startWorkflowRun}
      onCancel={cancelRun}
      onSetScheduleEnabled={setScheduleEnabled}
      runActionsEnabled={runActionsEnabled}
      inputDraft={!historical ? inputDraft : undefined}
      onEditInput={() => openNode(INPUT_NODE_ID)}
    />
  ) : undefined;
  // Machine-only actions sit beside the normal run controls (Cancel).
  const runControlsPanel =
    machineRunId && normalRunControls ? (
      <div className="flex items-center gap-2 max-[700px]:flex-wrap">
        <MachineRunActions
          api={api}
          runId={machineRunId}
          instance={machineInstance}
          enabled={runActionsEnabled}
          onSend={openSend}
          onChanged={() => {
            machineRead.refresh();
            machineHistory.refresh();
          }}
        />
        {normalRunControls}
      </div>
    ) : (
      normalRunControls
    );
  const liveEventDescriptorKey =
    displayedRunId && inspectedNode ? `${displayedRunId}:${inspectedNode}` : "";
  const workspaceStyle = {
    "--workspace-inspector-width": `${inspectorWidth}px`,
    "--workspace-inspector-column-width": inspectorOpen ? `${inspectorWidth}px` : "0px",
    "--workspace-inspector-divider-width": "0px",
  } as CSSProperties;

  // Do not paint a definition/empty graph before the initial selected snapshot.
  if (
    historical &&
    !run &&
    state.selectedRunStatus !== "error" &&
    loadingFallback !== undefined
  )
    return loadingFallback;

  return (
    <section
      className={`avalanche-operator-ui avalanche-workspace flex min-h-0 min-w-0 flex-col ${className}`}
      style={{ height: "100%" }}
    >
      <div
        className={`workflow-workspace relative grid min-h-0 min-w-0 w-full flex-1 overflow-hidden grid-cols-[minmax(0,1fr)_var(--workspace-inspector-divider-width)_var(--workspace-inspector-column-width)] max-[1000px]:grid-cols-[minmax(0,1fr)] ${inspectorOpen ? "with-inspector" : ""}`}
        style={workspaceStyle}
      >
        <section className="canvas-shell relative col-start-1 flex min-h-0 min-w-0 flex-row overflow-hidden bg-canvas-shell">
          {runListPanel && (
            <div
              ref={timelineElement}
              style={
                timelineMaxHeight === undefined
                  ? undefined
                  : { maxHeight: timelineMaxHeight, overflowY: "auto" }
              }
              className={`workspace-timeline z-20 ${timelineExpanded ? "relative w-[410px] max-w-full shrink-0 max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:left-0" : "absolute top-3.5 left-3.5 w-[300px] max-w-[calc(100vw-3rem)]"}`}
            >
              {runListPanel}
            </div>
          )}
          <div
            className={`canvas relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden ${run ? "run-canvas bg-run-canvas" : "blueprint-canvas bg-panel"}`}
          >
            {workflow || run ? (
              <>
                <div
                  ref={graphShellElement}
                  className={`run-graph-shell relative min-w-0 flex-1 overflow-hidden ${machineRunId ? "min-h-[300px]" : "min-h-0"}`}
                >
                  <GraphCanvas
                    machine={visitRun || visitExpired ? undefined : machineView}
                    workflow={!run || previewWorkflow ? workflow : undefined}
                    runTopology={
                      visitRun
                        ? visitRun.topology
                        : previewWorkflow || visitExpired
                          ? undefined
                          : run?.topology
                    }
                    runNodes={
                      visitRun
                        ? visitRun.nodes
                        : requestingRun || visitExpired
                          ? undefined
                          : run?.nodes
                    }
                    expansions={visitRun ? visitRun.expansions : run?.expansions}
                    runTriggeredBy={previewWorkflow ? undefined : run?.summary?.triggeredBy}
                    inspectionDisabled={requestingRun}
                    evaluations={evaluationState.records}
                    selectedNodeId={inspectedNode}
                    onClearNode={closePanel}
                    onOpenNode={openNode}
                    bottomRightPanel={
                      bottomRightPanel === undefined ? runControlsPanel : bottomRightPanel
                    }
                    reserve={machineSpec ? timelineReserve : undefined}
                  />
                  {evaluationState.error && (
                    <p
                      role="alert"
                      className="absolute bottom-4 left-4 z-10 m-0 text-xs text-danger"
                    >
                      {evaluationState.error}
                    </p>
                  )}
                  {historical && !loadedRun && (
                    <div
                      className="absolute right-3 top-3 max-w-[240px] rounded border border-line bg-panel p-3 text-xs text-secondary"
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
                  {requestingRun ? (
                    <div
                      className="pointer-events-none absolute top-[18px] right-[18px] z-[5] max-w-[240px] rounded border border-line bg-panel px-3 py-2.5 text-xs text-secondary shadow-sm max-[700px]:top-auto max-[700px]:bottom-16"
                      role="status"
                      aria-label="Run preparation"
                    >
                      <span className="flex items-center gap-2 font-medium text-ink">
                        <span
                          className="size-3 animate-spin rounded-full border-2 border-line border-t-amber motion-reduce:animate-none"
                          aria-hidden="true"
                        />
                        Requesting run
                      </span>
                      <span className="mt-1 block">
                        Preparing execution. Nodes will become available when ready.
                      </span>
                    </div>
                  ) : run && (runExpired || visitExpired) ? (
                    <div
                      role="status"
                      data-run-history="expired"
                      className="historical-badge pointer-events-none absolute top-[18px] right-[18px] z-[5] max-w-[280px] rounded border border-history-line bg-history-surface px-3 py-[9px] text-[9px] text-history-ink shadow-[0_4px_14px_var(--color-shadow-4)] max-[700px]:hidden [&>span]:mb-[3px] [&>span]:block [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-amber [&>span]:uppercase"
                    >
                      <span>expired</span>
                      {visitExpired && !runExpired ? "This visit's" : "This run's"} history and
                      artifacts expired under the run&apos;s retention policy. The run and its
                      label remain.
                    </div>
                  ) : run && !runLive ? (
                    <div className="historical-badge pointer-events-none absolute top-[18px] right-[18px] z-[5] rounded border border-history-line bg-history-surface px-3 py-[9px] text-[9px] text-history-ink shadow-[0_4px_14px_var(--color-shadow-4)] max-[700px]:hidden [&>span]:mb-[3px] [&>span]:block [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-amber [&>span]:uppercase">
                      <span>Viewing a run snapshot</span>This view does not represent the
                      workflow&apos;s current state.
                    </div>
                  ) : null}
                </div>
                {run ? (
                  <RunLogPane
                    height={layout.logHeight}
                    expanded={layout.logExpanded}
                    onHeightChange={setLogHeight}
                    onExpandedChange={setLogExpanded}
                    api={api}
                    run={visitRun ?? run}
                    nodeId={inspectedNode === INPUT_NODE_ID ? undefined : inspectedNode}
                    liveLogs={
                      displayedRunId && !visitRun ? state.liveLogs[displayedRunId] : undefined
                    }
                    onSelectNode={openNode}
                  />
                ) : (
                  emptyRunPanel
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
              max={maximumInspectorWidth()}
              pointerDirection={-1}
              onChange={setInspectorWidth}
            />
            <div
              id={`${workspaceId}-inspector`}
              className="workspace-inspector-pane col-start-3 grid min-h-0 min-w-0 overflow-hidden bg-panel max-[1000px]:absolute max-[1000px]:inset-y-0 max-[1000px]:right-0 max-[1000px]:z-30 max-[1000px]:w-[var(--workspace-inspector-width)] max-[1000px]:max-w-full max-[700px]:w-full"
            >
              {nodeInspectorOpen || !machineRunId ? (
                <Inspector
                  api={api}
                  embedded
                  workflow={workflow}
                  run={visitRun ?? run}
                  nodeId={inspectedNode}
                  evaluationState={evaluationState}
                  liveEvents={state.liveEvents[liveEventDescriptorKey]}
                  liveClassifierEvents={state.liveClassifierEvents[liveEventDescriptorKey]}
                  onClose={closePanel}
                  definitionLabel={definitionLabel}
                  onOpenDefinition={
                    run &&
                    !visitRun &&
                    inspectedNode &&
                    workflow?.nodeIds.includes(inspectedNode)
                      ? openCurrentDefinition
                      : undefined
                  }
                  inputDraft={!historical && runActionsEnabled ? inputDraft : undefined}
                />
              ) : (
                <MachineSendPanel
                  key={machineRunId}
                  api={api}
                  runId={machineRunId}
                  workflow={workflow!}
                  spec={machineSpec!}
                  instance={machineInstance}
                  onClose={() => setSendFor(undefined)}
                  onSent={() => {
                    machineRead.refresh();
                    machineHistory.refresh();
                  }}
                />
              )}
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
          <div className="flex items-center gap-2.5 rounded border border-on-notice-line/10 bg-notice-surface px-3.5 py-2.5 text-[11px] font-medium tracking-[-0.01em] text-notice-ink shadow-[0_10px_30px_var(--color-shadow-5)]">
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
  loadingFallback,
  emptyRunPanel,
  navigation,
  currentLabel,
  currentRowAction,
  showCurrentRow,
  deployments,
  timelineView,
  onTimelineViewChange,
  layoutStorageKey,
}: WorkflowWorkspaceProps) {
  const { state, startRun, cancelRun, selectRun, setScheduleEnabled } =
    useOperatorProjection(api);
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
    if (loadingFallback !== undefined) return loadingFallback;
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
      key={layoutStorageKey}
      layoutStorageKey={layoutStorageKey}
      api={api}
      state={state}
      workflowId={workflowId}
      workflow={workflow}
      selectedRunId={selectedRunId}
      onSelectRun={navigation ? navigation.onSelectRun : onSelectRun}
      selectRun={selectRun}
      startRun={startRun}
      cancelRun={cancelRun}
      setScheduleEnabled={setScheduleEnabled}
      className={className}
      runActionsEnabled={runActionsEnabled}
      definitionLabel={definitionLabel}
      bottomRightPanel={bottomRightPanel}
      loadingFallback={loadingFallback}
      emptyRunPanel={emptyRunPanel}
      currentLabel={currentLabel}
      currentRowAction={currentRowAction}
      showCurrentRow={showCurrentRow}
      deployments={deployments}
      timelineView={timelineView}
      onTimelineViewChange={onTimelineViewChange}
    />
  );
}
