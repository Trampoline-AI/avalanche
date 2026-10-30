import { ChevronDown, Clock } from "lucide-react";
import { useEffect, useLayoutEffect, useState } from "react";

import { describeCron, formatCountdown } from "./cron";
import type { FlowInfoMsg, InputFileAttachment, RunSnapshotMsg } from "./model";
import { applyInputFiles, type InputDraft } from "./useInputDraft";
import { workflowInputDraft } from "./workflowInput";
import type { OperatorProjection } from "./state";
import { isUnknownRecord } from "./guards";

interface RunControlsProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  pending?: OperatorProjection["action"];
  onStart: (
    workflowSelector: string,
    input?: Record<string, unknown>,
    inputFiles?: InputFileAttachment[],
  ) => Promise<string>;
  onCancel: (runId: string) => Promise<void>;
  onSetScheduleEnabled?: (
    workflowSelector: string,
    enabled: boolean,
    input?: Record<string, unknown>,
    inputFiles?: InputFileAttachment[],
  ) => Promise<void>;
  runActionsEnabled?: boolean;
  /** The workflow's input draft, edited in the input panel. */
  inputDraft?: InputDraft;
  /** Opens the input panel, offered next to errors when the workflow takes input. */
  onEditInput?: () => void;
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

/** Input JSON and the file attachments that fill its file fields. */
interface StartInput {
  input?: Record<string, unknown>;
  /** Absent when no file field has picked files. */
  inputFiles?: InputFileAttachment[];
}

export function parseRunInput(draft: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(draft);
  if (!isUnknownRecord(parsed)) throw new Error("Run input must be a JSON object");
  return parsed;
}

export function RunControls({
  workflow,
  run,
  pending,
  onStart,
  onCancel,
  onSetScheduleEnabled,
  runActionsEnabled = true,
  inputDraft,
  onEditInput,
}: RunControlsProps) {
  const [showSchedule, setShowSchedule] = useState(false);
  // One input for both Run and the cron schedule (see useInputDraft).
  const draft = inputDraft?.text ?? "{}";
  const pickedFiles = inputDraft?.files ?? {};
  const filesPicked = Object.keys(pickedFiles).length > 0;
  const draftEdited =
    inputDraft !== undefined && (inputDraft.text !== inputDraft.seed || filesPicked);
  const takesInput = Boolean(workflow?.inputDeclarationJson);
  // Fields whose typed text does not parse yet; their draft value is stale.
  const invalidFields = Object.keys(inputDraft?.pending ?? {});
  const refuseInvalid = (): boolean => {
    if (invalidFields.length === 0) return false;
    setError(`Fix the input before starting: ${invalidFields.join(", ")} is not valid JSON`);
    return true;
  };
  const [error, setError] = useState<string>();
  const workflowId = workflow?.workflowId;
  useLayoutEffect(() => {
    setShowSchedule(false);
    setError(undefined);
  }, [workflowId]);

  const scheduled = Boolean(workflow?.cron) && onSetScheduleEnabled !== undefined;
  const scheduleEnabled = workflow?.scheduleEnabled ?? false;
  const scheduleBusy = pending?.kind === "schedule";

  /** The draft's input, with its picked files when it has any. */
  const draftStart = (): StartInput =>
    filesPicked
      ? applyInputFiles(parseRunInput(draft), pickedFiles)
      : { input: parseRunInput(draft) };

  /** The edited input, or no input to keep the operator's own (defaults or stored). */
  const editedInput = (): StartInput => (draftEdited ? draftStart() : {});

  /**
   * The input a manual run starts with. A manual run never uses the schedule's
   * stored input, so send the draft whenever it holds a value, including when it was
   * seeded from the schedule's input.
   */
  const runInput = (): StartInput => {
    if (!inputDraft || !workflow) return {};
    if (filesPicked) return draftStart();
    // The draft holds only set values; an empty one lets the run take every default.
    const input = parseRunInput(draft);
    return Object.keys(input).length > 0 ? { input } : {};
  };

  const setSchedule = async (enabled: boolean, start: StartInput = {}) => {
    if (!workflow || !onSetScheduleEnabled) return;
    try {
      await onSetScheduleEnabled(workflow.workflowId, enabled, start.input, start.inputFiles);
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
    if (refuseInvalid()) return;
    let start: StartInput;
    try {
      start = editedInput();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Run input is invalid JSON");
      return;
    }
    void setSchedule(true, start);
  };

  // Offer to update an enabled schedule when the edited input differs from its input.
  let draftDiffersFromSchedule = false;
  if (scheduled && scheduleEnabled && draftEdited && workflow) {
    try {
      draftDiffersFromSchedule =
        filesPicked ||
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
    if (refuseInvalid()) return;
    let start: StartInput;
    try {
      start = runInput();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Run input is invalid JSON");
      return;
    }
    try {
      await onStart(workflow.workflowId, start.input, start.inputFiles);
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
              {scheduleEnabled && workflow.nextRunAt > 0 && (
                <NextRunCountdown at={workflow.nextRunAt} />
              )}
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
              {draftDiffersFromSchedule && (
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0.5 text-[9px] text-acid underline underline-offset-2 hover:text-acid-hover"
                  disabled={scheduleBusy}
                  onClick={() => {
                    setError(undefined);
                    if (refuseInvalid()) return;
                    let start: StartInput;
                    try {
                      start = draftStart();
                    } catch (reason) {
                      setError(
                        reason instanceof Error ? reason.message : "Run input is invalid JSON",
                      );
                      return;
                    }
                    void setSchedule(true, start);
                  }}
                >
                  Use for schedule
                </button>
              )}
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
      {(error || (workflow && runActionsEnabled && scheduled && showSchedule)) && (
        <div className="run-popovers absolute right-0 bottom-[43px] z-20 flex w-[390px] flex-col gap-2 max-[700px]:w-[calc(100vw-32px)]">
          {error && (
            <div
              role="alert"
              className="action-error flex items-baseline gap-3 rounded border border-error-line bg-error-surface px-[18px] py-2 text-xs text-error-ink"
            >
              <span className="min-w-0 flex-1">{error}</span>
              {takesInput && onEditInput && (
                <button
                  type="button"
                  className="shrink-0 cursor-pointer border-0 bg-transparent p-0 text-[11px] text-error-ink underline underline-offset-2 hover:text-ink"
                  onClick={onEditInput}
                >
                  Edit inputs
                </button>
              )}
            </div>
          )}
          {workflow && runActionsEnabled && scheduled && showSchedule && (
            <p className="schedule-summary m-0 self-end rounded border border-line-strong bg-panel px-3 py-2 text-[11px] text-ink shadow-[0_18px_50px_var(--color-shadow-1)]">
              {describeCron(workflow.cron) ?? "Custom schedule"}{" "}
              <code className="font-mono text-secondary">({workflow.cron})</code>
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Time left until the next scheduled run, ticking every second. */
function NextRunCountdown({ at }: { at: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const remaining = at - now / 1000;
  const next = new Date(at * 1000);
  return (
    <time
      className="schedule-countdown font-mono text-[10px] text-secondary tabular-nums"
      dateTime={next.toISOString()}
      title={`Next run ${next.toLocaleString()}`}
    >
      {remaining > 0 ? `in ${formatCountdown(remaining)}` : "starting…"}
    </time>
  );
}
