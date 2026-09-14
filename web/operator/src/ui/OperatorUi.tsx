import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { LoaderCircle, PanelLeftOpen } from "lucide-react";

import { Explorer } from "../Explorer";
import { WorkflowWorkspaceSurface } from "../WorkflowWorkspace";
import { WorkspaceDivider } from "../WorkspaceDivider";
import { useOperatorProjection } from "../state";
import type { OperatorUiProps, OperatorUiSelection } from "./types";

const EXPLORER_MIN_WIDTH = 220;
const EXPLORER_MAX_WIDTH = 420;
const EXPLORER_DEFAULT_WIDTH = 280;
export function OperatorUi({ host, navigation }: OperatorUiProps) {
  const { api, presentation } = host;
  const { state, startRun, cancelRun, selectRun } = useOperatorProjection(api);
  const [localSelection, setLocalSelection] = useState<OperatorUiSelection>();
  const selection = navigation ? navigation.selection : localSelection;
  const workflow = state.catalog?.workflows.find(
    (item) => item.workflowId === selection?.workflowId,
  );
  const setSelection = useCallback(
    (next: OperatorUiSelection | undefined) => {
      if (navigation) {
        navigation.onSelectionChange(next);
        return;
      }
      setLocalSelection(next);
    },
    [navigation],
  );
  const [explorerOpen, setExplorerOpen] = useState(false);
  const [explorerCollapsed, setExplorerCollapsed] = useState(false);
  const [explorerWidth, setExplorerWidth] = useState(EXPLORER_DEFAULT_WIDTH);
  useEffect(() => {
    if (!state.catalog) return;
    if (
      selection &&
      state.catalog.workflows.some((item) => item.workflowId === selection.workflowId)
    )
      return;
    const first = state.catalog.workflows[0];
    if (first) setSelection({ kind: "workflow", workflowId: first.workflowId });
    else if (selection) setSelection(undefined);
  }, [selection, setSelection, state.catalog]);

  const collapseExplorer = useCallback(() => setExplorerCollapsed(true), []);
  const restoreExplorer = useCallback(() => setExplorerCollapsed(false), []);
  const select = useCallback(
    (next: OperatorUiSelection) => {
      if (next.workflowId !== selection?.workflowId) void selectRun(undefined);
      setSelection(next);
      setExplorerOpen(false);
    },
    [selectRun, selection?.workflowId, setSelection],
  );
  const selectWorkflowRun = useCallback(
    (runId: string | undefined) => {
      if (!workflow) return;
      setSelection(
        runId
          ? { kind: "run", workflowId: workflow.workflowId, runId }
          : { kind: "workflow", workflowId: workflow.workflowId },
      );
    },
    [setSelection, workflow],
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
  const workspaceStyle = {
    "--workspace-explorer-width": `${explorerWidth}px`,
    "--workspace-explorer-column-width": explorerCollapsed ? "0px" : `${explorerWidth}px`,
    "--workspace-explorer-divider-width": "0px",
  } as CSSProperties;
  if (!state.catalog) {
    return (
      <div className="avalanche-operator-ui">
        <main
          className="operator-connection-screen grid min-h-screen w-full place-items-center bg-canvas p-6 text-center"
          role="status"
          aria-live="polite"
        >
          <div className="grid justify-items-center gap-4">
            <LoaderCircle
              aria-hidden="true"
              className="size-7 animate-spin text-acid motion-reduce:animate-none"
            />
            <div>
              <h1 className="text-lg font-semibold tracking-[-0.02em] text-ink">
                Reconnecting...
              </h1>
              <p className="mt-2 text-sm text-muted">{presentation.unavailableDescription}</p>
            </div>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="avalanche-operator-ui">
      <div
        className={`app-shell flex h-full flex-col ${explorerOpen ? "explorer-open" : ""} ${
          explorerCollapsed ? "explorer-collapsed" : ""
        }`}
      >
        <header className="topbar relative z-10 grid min-h-[58px] grid-cols-[260px_minmax(0,1fr)_auto_auto] items-center border-b border-line bg-white px-5 shadow-[0_1px_2px_rgba(20,31,26,.04)] max-[1000px]:grid-cols-[210px_minmax(0,1fr)_auto_auto] max-[700px]:grid-cols-[auto_minmax(0,1fr)_auto] max-[700px]:gap-2 max-[700px]:px-2.5">
          <div className="brand flex items-center gap-[11px]">
            <img
              className="brand-mark size-[30px] object-contain"
              src={presentation.brandImageUrl}
              alt=""
            />
            <div className="flex items-baseline gap-[7px]">
              <strong className="text-[15px] tracking-[-0.02em]">Avalanche</strong>
              <span className="font-mono text-[11px] text-muted uppercase">Operator</span>
            </div>
          </div>
          <div className="breadcrumb absolute left-1/2 flex -translate-x-1/2 justify-center gap-[9px] text-xs text-muted max-[700px]:hidden [&_i]:opacity-40 [&_strong]:font-semibold [&_strong]:text-[#26322c]">
            <span>{workflow?.rootAlias || presentation.rootLabel}</span>
            {workflow && (
              <>
                <i>/</i>
                <strong>{workflow.displayName}</strong>
              </>
            )}
            {selection?.kind === "run" && (
              <>
                <i>/</i>
                <strong>{selection.runId}</strong>
              </>
            )}
          </div>
          <div
            className={`connection flex items-center gap-2 font-mono text-[11px] capitalize [&>span]:size-[7px] [&>span]:rounded-full ${state.connection === "live" ? "[&>span]:bg-mint" : "[&>span]:bg-amber"} max-[700px]:justify-self-end connection-${state.connection}`}
          >
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
        {state.error && (
          <div className="connection-error border-b border-[#efb9b5] bg-[#fff1f0] px-[18px] py-2 text-xs text-[#9d2923]">
            {state.error}
          </div>
        )}
        <main
          className="workspace grid min-h-0 w-full flex-1 overflow-hidden grid-cols-[var(--workspace-explorer-column-width)_var(--workspace-explorer-divider-width)_minmax(0,1fr)] max-[700px]:grid-cols-[minmax(0,1fr)]"
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
          {(workflow || state.catalog.workflows.length === 0) && (
            <WorkflowWorkspaceSurface
              api={api}
              state={state}
              workflow={workflow}
              selectedRunId={selection?.kind === "run" ? selection.runId : undefined}
              onSelectRun={selectWorkflowRun}
              selectRun={selectRun}
              startRun={startRun}
              cancelRun={cancelRun}
              leadingRunPanel={restoreButton}
              workflowReloadDescription={presentation.workflowReloadDescription}
              className="col-start-3 max-[700px]:col-start-1"
            />
          )}
        </main>
      </div>
    </div>
  );
}
