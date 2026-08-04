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
  return <div className="json-editor" ref={parent} />;
}

interface RunControlsProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  pending?: { kind: "start" | "cancel"; target: string };
  onStart: (workflowSelector: string, input?: Record<string, unknown>) => Promise<string>;
  onCancel: (runId: string) => Promise<void>;
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
}: RunControlsProps) {
  const [showInput, setShowInput] = useState(false);
  const [draft, setDraft] = useState("{}");
  const [error, setError] = useState<string>();
  const active = run?.summary?.status === "pending" || run?.summary?.status === "running";

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
    <div className="run-controls">
      {workflow && (
        <>
          <button
            type="button"
            className="run-button"
            disabled={pending?.kind === "start"}
            onClick={() => void start()}
          >
            <svg aria-hidden="true" viewBox="0 0 12 12">
              <path d="M2.5 1.5 10 6l-7.5 4.5z" />
            </svg>
            <span>{pending?.kind === "start" ? "Requesting…" : "Run"}</span>
          </button>
          <button
            type="button"
            className={`input-toggle ${showInput ? "active" : ""}`}
            onClick={() => setShowInput((value) => !value)}
          >
            {showInput ? "Hide JSON input" : "Add JSON input"}
          </button>
        </>
      )}
      {active && run?.summary && (
        <button
          type="button"
          className="cancel-button"
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
        <div className="input-popover">
          <div>
            <strong>Workflow input</strong>
            <span>Schema-blind JSON object</span>
          </div>
          <JsonEditor value={draft} onChange={setDraft} />
        </div>
      )}
      {error && <div className="action-error">{error}</div>}
    </div>
  );
}
