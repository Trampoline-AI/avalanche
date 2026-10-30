import { json } from "@codemirror/lang-json";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { ChevronDown, Clock } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { FlowInfoMsg, RunSnapshotMsg, RunSummaryMsg } from "./model";
import { ScheduleDetails } from "./ScheduleDetails";
import { workflowInputDraft } from "./workflowInput";
import type { OperatorProjection } from "./state";
import { isUnknownRecord } from "./guards";

interface JsonEditorProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
}

export function JsonEditor({
  value,
  onChange,
  label = "Workflow input JSON",
}: JsonEditorProps) {
  const parent = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!parent.current) return;
    const view = new EditorView({
      parent: parent.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          json(),
          keymap.of([]),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({
            "aria-label": label,
          }),
          EditorView.theme({
            "&": { backgroundColor: "var(--color-panel)", color: "var(--color-ink)" },
            ".cm-content": { caretColor: "var(--color-acid)", minHeight: "110px" },
            ".cm-gutters": {
              backgroundColor: "var(--color-canvas)",
              color: "var(--color-code-gutter)",
              border: "0",
            },
            "&.cm-focused": { outline: "1px solid var(--color-code-focus)" },
          }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChange(update.state.doc.toString());
          }),
        ],
      }),
    });
    return () => view.destroy();
  }, []);
  return (
    <div
      className="json-editor overflow-hidden rounded border border-line text-[10px]"
      ref={parent}
    />
  );
}

interface RunControlsProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  /** Run history, for the schedule details' last scheduled run. */
  runs?: Record<string, RunSummaryMsg>;
  pending?: OperatorProjection["action"];
  onStart: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  onCancel: (runId: string) => Promise<void>;
  onSetScheduleEnabled?: (
    workflowSelector: string,
    enabled: boolean,
    input?: Record<string, unknown>,
  ) => Promise<void>;
  onSelectRun?: (runId: string) => void;
  runActionsEnabled?: boolean;
}

function inputText(workflow: FlowInfoMsg | undefined): string {
  return JSON.stringify((workflow && workflowInputDraft(workflow)) ?? {}, null, 2);
}

/** Key-order independent JSON, to tell whether two inputs differ. */
function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    isUnknownRecord(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([left], [right]) => (left < right ? -1 : 1)),
        )
      : item,
  );
}

export function parseRunInput(draft: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(draft);
  if (!isUnknownRecord(parsed)) throw new Error("Run input must be a JSON object");
  return parsed;
}

export function RunControls({
  workflow,
  run,
  runs = {},
  pending,
  onStart,
  onCancel,
  onSetScheduleEnabled,
  onSelectRun,
  runActionsEnabled = true,
}: RunControlsProps) {
  const [showInput, setShowInput] = useState(false);
  const [showSchedule, setShowSchedule] = useState(false);
  // One input for both Run and the cron schedule, starting from the input the
  // schedule already uses or the workflow's declared defaults.
  const seed = inputText(workflow);
  const [draft, setDraft] = useState(seed);
  // Bumped whenever the draft is reseeded, to remount the editor with it.
  const [draftGeneration, setDraftGeneration] = useState(0);
  const [error, setError] = useState<string>();
  const workflowId = workflow?.workflowId;
  const draftWorkflow = useRef(workflowId);
  const seededDraft = useRef(seed);
  useLayoutEffect(() => {
    if (!runActionsEnabled) {
      setShowInput(false);
    }
  }, [runActionsEnabled]);
  useLayoutEffect(() => {
    const previousSeed = seededDraft.current;
    seededDraft.current = seed;
    if (draftWorkflow.current !== workflowId) {
      draftWorkflow.current = workflowId;
      setDraft(seed);
      setDraftGeneration((generation) => generation + 1);
      setShowSchedule(false);
      setError(undefined);
      return;
    }
    // The stored schedule input or the input schema changed elsewhere: follow it
    // unless the user has edited the draft.
    if (seed !== previousSeed && draft === previousSeed) {
      setDraft(seed);
      setDraftGeneration((generation) => generation + 1);
    }
  }, [draft, seed, workflowId]);

  const scheduled = Boolean(workflow?.cron) && onSetScheduleEnabled !== undefined;
  const scheduleEnabled = workflow?.scheduleEnabled ?? false;
  const scheduleBusy = pending?.kind === "schedule";

  /** The edited input, or undefined when the input editor is closed. */
  const editedInput = (): Record<string, unknown> | undefined =>
    showInput ? parseRunInput(draft) : undefined;

  const setSchedule = async (enabled: boolean, input?: Record<string, unknown>) => {
    if (!workflow || !onSetScheduleEnabled) return;
    try {
      await onSetScheduleEnabled(workflow.workflowId, enabled, input);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Operator rejected the change");
    }
  };

  const toggleSchedule = () => {
    setError(undefined);
    if (scheduleEnabled) {
      void setSchedule(false);
      return;
    }
    let input: Record<string, unknown> | undefined;
    try {
      input = editedInput();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Run input is invalid JSON");
      return;
    }
    void setSchedule(true, input);
  };

  // Offer to update an enabled schedule when the edited input differs from its input.
  let draftDiffersFromSchedule = false;
  if (scheduled && scheduleEnabled && showInput && workflow) {
    try {
      draftDiffersFromSchedule =
        canonicalJson(parseRunInput(draft)) !==
        canonicalJson(workflowInputDraft(workflow) ?? {});
    } catch {
      draftDiffersFromSchedule = false;
    }
  }

  const active =
    run?.summary?.status === "requesting" ||
    run?.summary?.status === "pending" ||
    run?.summary?.status === "running";

  const start = async () => {
    if (!workflow) return;
    setError(undefined);
    let input: Record<string, unknown> | undefined;
    try {
      input = editedInput();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Run input is invalid JSON");
      return;
    }
    try {
      await onStart(workflow.workflowId, input);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Operator rejected the run");
    }
  };

  return (
    <div className="run-controls relative flex items-center gap-2 max-[700px]:flex-wrap [&_button:disabled]:cursor-wait [&_button:disabled]:opacity-50">
      {workflow && runActionsEnabled && (
        <>
          <button
            type="button"
            className="run-button inline-flex cursor-pointer items-center gap-1.5 rounded border border-acid bg-acid px-[11px] py-[7px] text-[10px] font-bold text-on-accent hover:border-acid-hover hover:bg-acid-hover [&_svg]:size-[11px] [&_svg]:fill-current"
            onClick={() => void start()}
          >
            <svg aria-hidden="true" viewBox="0 0 12 12">
              <path d="M2.5 1.5 10 6l-7.5 4.5z" />
            </svg>
            <span>Run</span>
          </button>
          <button
            type="button"
            className={`input-toggle cursor-pointer border-0 bg-transparent p-0.5 text-[9px] text-acid underline underline-offset-2 hover:text-acid-hover ${showInput ? "active text-acid-hover" : ""}`}
            onClick={() => setShowInput((value) => !value)}
          >
            {showInput ? "Hide JSON input" : "Add JSON input"}
          </button>
          {scheduled && (
            <span className="schedule-controls ml-1 flex items-center gap-1.5 border-l border-line pl-2.5 text-[10px] text-ink">
              <button
                type="button"
                role="switch"
                aria-checked={scheduleEnabled}
                aria-label="Run on schedule"
                disabled={scheduleBusy}
                onClick={toggleSchedule}
                className={`relative h-4 w-7 shrink-0 cursor-pointer rounded-full border-0 p-0 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid ${scheduleEnabled ? "bg-acid" : "bg-line-strong"}`}
              >
                <span
                  aria-hidden="true"
                  className={`absolute top-0.5 size-3 rounded-full bg-on-accent shadow transition-[left] ${scheduleEnabled ? "left-3.5" : "left-0.5"}`}
                />
              </button>
              <button
                type="button"
                aria-expanded={showSchedule}
                aria-label="Schedule details"
                className="inline-flex cursor-pointer items-center gap-1 border-0 bg-transparent p-0.5 text-[10px] text-ink hover:text-acid"
                onClick={() => setShowSchedule((value) => !value)}
              >
                <Clock aria-hidden="true" className="size-3 text-secondary" />
                <span aria-hidden="true">Schedule</span>
                <ChevronDown
                  aria-hidden="true"
                  className={`size-3 transition-transform ${showSchedule ? "rotate-180" : ""}`}
                />
              </button>
            </span>
          )}
        </>
      )}
      {active && run?.summary && runActionsEnabled && (
        <button
          type="button"
          className="cancel-button cursor-pointer rounded border border-cancel-line bg-panel px-[11px] py-[7px] text-[10px] text-cancel-ink hover:bg-cancel-hover"
          disabled={pending?.kind === "cancel"}
          onClick={() => {
            setError(undefined);
            void onCancel(run.summary!.runId).catch((reason: unknown) => {
              setError(reason instanceof Error ? reason.message : "Cancellation failed");
            });
          }}
        >
          {pending?.kind === "cancel" ? "Cancelling…" : "Cancel run"}
        </button>
      )}
      {run?.terminalSeal && (
        <div
          className="terminal-seal flex items-center gap-1.5 rounded border border-line-strong bg-canvas px-[11px] py-[7px] text-[10px] text-secondary"
          data-terminal-status={run.terminalSeal.terminalStatus}
        >
          <span className="terminal-seal-status font-bold">
            Terminal: {run.terminalSeal.terminalStatus}
          </span>
          {run.terminalSeal.reason !== undefined && (
            <span className="terminal-seal-reason">Reason: {run.terminalSeal.reason}</span>
          )}
        </div>
      )}
      {(error ||
        (workflow && runActionsEnabled && (showInput || (scheduled && showSchedule)))) && (
        <div className="run-popovers absolute right-0 bottom-[43px] z-20 flex w-[390px] flex-col gap-2 max-[700px]:w-[calc(100vw-32px)]">
          {error && (
            <div
              role="alert"
              className="action-error rounded border border-error-line bg-error-surface px-[18px] py-2 text-xs text-error-ink"
            >
              {error}
            </div>
          )}
          {workflow && runActionsEnabled && scheduled && showSchedule && (
            <ScheduleDetails workflow={workflow} runs={runs} onSelectRun={onSelectRun} />
          )}
          {workflow && runActionsEnabled && showInput && (
            <div className="input-popover rounded border border-line-strong bg-panel p-[13px] shadow-[0_18px_50px_var(--color-shadow-1)]">
              <div className="mb-[9px] flex items-center justify-between gap-2">
                <strong className="text-[11px]">Workflow input</strong>
                {draftDiffersFromSchedule && (
                  <button
                    type="button"
                    className="cursor-pointer border-0 bg-transparent p-0 text-[9px] text-acid underline underline-offset-2 hover:text-acid-hover"
                    disabled={scheduleBusy}
                    onClick={() => {
                      setError(undefined);
                      void setSchedule(true, parseRunInput(draft));
                    }}
                  >
                    Use for schedule
                  </button>
                )}
              </div>
              <JsonEditor
                value={draft}
                onChange={setDraft}
                key={`${workflowId}:${draftGeneration}`}
              />
              {scheduled && (
                <p className="m-0 mt-1.5 text-[9px] text-secondary">
                  Run starts with this input. Turning the schedule on saves it for scheduled
                  runs.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
