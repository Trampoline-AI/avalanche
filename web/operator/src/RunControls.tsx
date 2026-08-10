import { json } from "@codemirror/lang-json";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { useEffect, useRef, useState } from "react";

import type { FlowInfoMsg, RunSnapshotMsg } from "./generated/operator";
import { isUnknownRecord } from "./guards";

interface JsonEditorProps {
  value: string;
  onChange: (value: string) => void;
}

function JsonEditor({ value, onChange }: JsonEditorProps) {
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
            "aria-label": "Workflow input JSON",
          }),
          EditorView.theme({
            "&": { backgroundColor: "#ffffff", color: "#17211c" },
            ".cm-content": { caretColor: "#2563eb", minHeight: "110px" },
            ".cm-gutters": { backgroundColor: "#f6f8f7", color: "#7b8680", border: "0" },
            "&.cm-focused": { outline: "1px solid #9bb6f5" },
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
      className="json-editor overflow-hidden rounded-[7px] border border-line text-[10px]"
      ref={parent}
    />
  );
}

interface RunControlsProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  pending?: { kind: "start" | "cancel"; target: string };
  onStart: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  onCancel: (runId: string) => Promise<void>;
  onViewWorkflow?: () => void;
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
  onViewWorkflow,
}: RunControlsProps) {
  const [showInput, setShowInput] = useState(false);
  const [draft, setDraft] = useState("{}");
  const [error, setError] = useState<string>();
  const active =
    run?.summary?.status === "requesting" ||
    run?.summary?.status === "pending" ||
    run?.summary?.status === "running";

  const start = async () => {
    if (!workflow) return;
    setError(undefined);
    let input: Record<string, unknown> | undefined;
    if (showInput) {
      try {
        input = parseRunInput(draft);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Run input is invalid JSON");
        return;
      }
    }
    try {
      await onStart(workflow.workflowId, input);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Operator rejected the run");
    }
  };

  return (
    <div className="run-controls relative flex items-center gap-2 max-[700px]:flex-wrap [&_button:disabled]:cursor-wait [&_button:disabled]:opacity-50">
      {onViewWorkflow && (
        <button
          type="button"
          className="workflow-view-button inline-flex cursor-pointer items-center gap-1.5 rounded-[7px] border border-line bg-white px-[11px] py-[7px] text-[10px] font-bold text-secondary hover:border-secondary hover:bg-[#f7f9f8] hover:text-ink [&_svg]:size-[11px]"
          title="Leave this immutable run and view the current workflow"
          onClick={onViewWorkflow}
        >
          <svg aria-hidden="true" viewBox="0 0 12 12" fill="none">
            <path
              d="M5 2 1.5 6 5 10M2 6h8"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span>Current workflow</span>
        </button>
      )}
      {workflow && (
        <>
          <button
            type="button"
            className="run-button inline-flex cursor-pointer items-center gap-1.5 rounded-[7px] border border-acid bg-acid px-[11px] py-[7px] text-[10px] font-bold text-white hover:border-[#1d4ed8] hover:bg-[#1d4ed8] [&_svg]:size-[11px] [&_svg]:fill-current"
            onClick={() => void start()}
          >
            <svg aria-hidden="true" viewBox="0 0 12 12">
              <path d="M2.5 1.5 10 6l-7.5 4.5z" />
            </svg>
            <span>Run</span>
          </button>
          <button
            type="button"
            className={`input-toggle cursor-pointer border-0 bg-transparent p-0.5 text-[9px] text-acid underline underline-offset-2 hover:text-[#1d4ed8] ${showInput ? "active text-[#1d4ed8]" : ""}`}
            onClick={() => setShowInput((value) => !value)}
          >
            {showInput ? "Hide JSON input" : "Add JSON input"}
          </button>
        </>
      )}
      {active && run?.summary && (
        <button
          type="button"
          className="cancel-button cursor-pointer rounded-[7px] border border-[#e0a6a1] bg-white px-[11px] py-[7px] text-[10px] text-[#a92f29] hover:bg-[#fff3f2]"
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
      {showInput && workflow && (
        <div className="input-popover absolute right-0 bottom-[43px] z-20 w-[390px] rounded-[9px] border border-[#cbd2ce] bg-white p-[13px] shadow-[0_18px_50px_rgba(20,31,26,.16)] max-[700px]:w-[calc(100vw-32px)] [&>div:first-child]:mb-[9px] [&>div:first-child]:flex [&>div:first-child]:justify-between [&_strong]:text-[11px] [&_span]:font-mono [&_span]:text-[8px] [&_span]:text-[#6d7872]">
          <div>
            <strong>Workflow input</strong>
          </div>
          <JsonEditor value={draft} onChange={setDraft} />
        </div>
      )}
      {error && (
        <div
          className={`action-error absolute right-0 z-[21] w-[390px] rounded-[7px] border border-[#efb9b5] bg-[#fff1f0] px-[18px] py-2 text-xs text-[#9d2923] max-[700px]:w-[calc(100vw-32px)] ${showInput ? "bottom-[205px]" : "bottom-11"}`}
        >
          {error}
        </div>
      )}
    </div>
  );
}
