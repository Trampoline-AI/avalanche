import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { LoaderCircle, PanelLeft } from "lucide-react";

import { Explorer } from "../Explorer";
import { WorkflowWorkspaceSurface } from "../WorkflowWorkspace";
import { WorkspaceDivider } from "../WorkspaceDivider";
import { useOperatorProjection } from "../state";
import type { OperatorUiProps, OperatorUiSelection } from "./types";

const EXPLORER_MIN_WIDTH = 220;
const EXPLORER_MAX_WIDTH = 420;
const EXPLORER_DEFAULT_WIDTH = 280;
const EXPLORER_RAIL_WIDTH = 48;
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
  const [explorerPinned, setExplorerPinned] = useState(true);
  const [explorerHovered, setExplorerHovered] = useState(false);
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

  const toggleExplorerPin = useCallback(() => {
    setExplorerPinned((pinned) => !pinned);
    setExplorerHovered(false);
  }, []);
  const previewExplorer = useCallback(() => {
    if (!explorerPinned) setExplorerHovered(true);
  }, [explorerPinned]);
  const closeExplorerPreview = useCallback(() => {
    if (!explorerPinned) setExplorerHovered(false);
  }, [explorerPinned]);
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
  const explorerExpanded = explorerPinned || explorerHovered;
  const workspaceStyle = {
    "--workspace-explorer-width": `${explorerWidth}px`,
    "--workspace-explorer-visible-width": explorerExpanded
      ? `${explorerWidth}px`
      : `${EXPLORER_RAIL_WIDTH}px`,
    "--workspace-explorer-column-width": explorerPinned
      ? `${explorerWidth}px`
      : `${EXPLORER_RAIL_WIDTH}px`,
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
          explorerPinned ? "explorer-pinned" : "explorer-unpinned"
        } ${explorerExpanded ? "explorer-expanded" : "explorer-collapsed"}`}
      >
        <header className="topbar relative z-10 grid min-h-[58px] grid-cols-[minmax(max-content,1fr)_minmax(0,max-content)_minmax(max-content,1fr)] items-center gap-x-4 border-b border-line bg-white px-5 shadow-[0_1px_2px_rgba(20,31,26,.04)] max-[700px]:grid-cols-[auto_minmax(0,1fr)_auto] max-[700px]:gap-2 max-[700px]:px-2.5">
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
          <div
            className="breadcrumb min-w-0 truncate py-1 text-left text-xs text-muted [direction:rtl] max-[700px]:hidden [&_i]:mx-[9px] [&_i]:opacity-40 [&_strong]:font-semibold [&_strong]:text-[#26322c]"
            title={workflow?.rootAlias || presentation.rootLabel}
          >
            <bdi dir="ltr">
              <span>{workflow?.rootAlias || presentation.rootLabel}</span>
              {workflow && (
                <>
                  <i>/</i>
                  <button
                    type="button"
                    aria-label={`View current state for ${workflow.displayName}`}
                    aria-current={selection?.kind === "workflow" ? "page" : undefined}
                    className="cursor-pointer border-0 bg-transparent p-0 text-inherit hover:underline focus-visible:rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
                    onClick={() => selectWorkflowRun(undefined)}
                  >
                    <strong>{workflow.displayName}</strong>
                  </button>
                </>
              )}
              {selection?.kind === "run" && (
                <>
                  <i>/</i>
                  <strong>{selection.runId}</strong>
                </>
              )}
            </bdi>
          </div>
          <div
            className={`connection flex items-center justify-self-end gap-2 font-mono text-[11px] whitespace-nowrap capitalize [&>span]:size-[7px] [&>span]:rounded-full ${state.connection === "live" ? "[&>span]:bg-mint" : "[&>span]:bg-amber"} connection-${state.connection}`}
          >
            <span />
            {state.connection === "live" ? "Live" : state.connection}
          </div>
          <button
            type="button"
            className="explorer-toggle hidden size-8 cursor-pointer items-center justify-center rounded-md border-0 bg-transparent text-muted hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid max-[700px]:flex"
            aria-label="Toggle workflows"
            aria-controls="operator-explorer"
            aria-expanded={explorerOpen}
            onClick={() => setExplorerOpen((open) => !open)}
          >
            <PanelLeft aria-hidden="true" className="size-4" />
          </button>
        </header>
        {state.error && (
          <div className="connection-error border-b border-[#efb9b5] bg-[#fff1f0] px-[18px] py-2 text-xs text-[#9d2923]">
            {state.error}
          </div>
        )}
        <main
          className="workspace relative grid min-h-0 w-full flex-1 overflow-hidden grid-cols-[var(--workspace-explorer-column-width)_var(--workspace-explorer-divider-width)_minmax(0,1fr)] max-[700px]:grid-cols-[minmax(0,1fr)]"
          style={workspaceStyle}
        >
          <Explorer
            catalog={state.catalog}
            selection={selection}
            onSelect={select}
            pinned={explorerPinned}
            expanded={explorerExpanded}
            open={explorerOpen}
            onTogglePin={toggleExplorerPin}
            onHoverStart={previewExplorer}
            onHoverEnd={closeExplorerPreview}
          />
          {explorerPinned && (
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
              workflowId={selection?.workflowId}
              workflow={workflow}
              selectedRunId={selection?.kind === "run" ? selection.runId : undefined}
              onSelectRun={selectWorkflowRun}
              selectRun={selectRun}
              startRun={startRun}
              cancelRun={cancelRun}
              workflowReloadDescription={presentation.workflowReloadDescription}
              definitionLabel={presentation.definitionLabel}
              className="col-start-3 max-[700px]:col-start-1"
            />
          )}
        </main>
      </div>
    </div>
  );
}
