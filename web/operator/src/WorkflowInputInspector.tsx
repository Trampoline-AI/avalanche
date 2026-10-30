import { useMemo } from "react";
import { SlidersHorizontal, X } from "lucide-react";

import type { FlowInfoMsg, RunSnapshotMsg } from "./model";
import type { InputDraft } from "./useInputDraft";
import { useInputFieldEditor, type InputFieldEditor } from "./useInputFieldEditor";
import { Markdown } from "./Markdown";
import { DeclaredSchema } from "./StepInterfacePanel";
import type { JsonSchema } from "./stepInterface";
import { ValueView } from "./ValueView";
import {
  decodeWorkflowInputDeclaration,
  formatInputValue,
  runInputFields,
  runTriggerLabel,
  scheduledInputNote,
  workflowInputFields,
  type RunInputField,
  type WorkflowInputField,
} from "./workflowInput";

interface WorkflowInputInspectorProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  embedded?: boolean;
  definitionLabel?: string;
  /** The input draft Run and the schedule start with; makes the fields editable. */
  inputDraft?: InputDraft;
  onClose: () => void;
}

type InspectedInput =
  | {
      typeName: string;
      jsonSchema: JsonSchema;
      fields: WorkflowInputField[];
      values?: RunInputField[];
    }
  | { error: string };

function FieldDefaultLabel({ field }: { field: WorkflowInputField }) {
  const fieldDefault = field.default;
  return (
    <span data-default-kind={fieldDefault.kind}>
      {fieldDefault.kind === "required" ? (
        <span className="text-[9px] text-amber uppercase">Required</span>
      ) : (
        <code className="font-mono text-[10px] text-muted [overflow-wrap:anywhere]">
          {fieldDefault.kind === "value"
            ? `default ${formatInputValue(fieldDefault.value)}`
            : "default computed per run"}
        </code>
      )}
    </span>
  );
}

/** The run input declaration, and for a run the values it started with. */
export function WorkflowInputInspector({
  workflow,
  run,
  embedded = false,
  definitionLabel = "Current definition",
  inputDraft,
  onClose,
}: WorkflowInputInspectorProps) {
  const rawDeclaration = run
    ? run.topology?.inputDeclarationJson
    : workflow?.inputDeclarationJson;
  const runInput = run?.input;
  const inspected = useMemo<InspectedInput>(() => {
    try {
      const declaration = decodeWorkflowInputDeclaration(rawDeclaration ?? "");
      if (!declaration) return { error: "This workflow declares no input." };
      return {
        typeName: declaration.typeName,
        jsonSchema: declaration.jsonSchema,
        fields: workflowInputFields(declaration),
        values: runInput ? runInputFields(declaration, runInput) : undefined,
      };
    } catch (error: unknown) {
      return {
        error: error instanceof Error ? error.message : "Input declaration unavailable",
      };
    }
  }, [rawDeclaration, runInput]);
  const triggeredBy = run?.summary?.triggeredBy;
  const cron = run ? "" : (workflow?.cron ?? "");
  const scheduleNote = run || !workflow ? undefined : scheduledInputNote(workflow);
  const panelLayout = embedded
    ? "relative h-full w-full"
    : "fixed right-0 bottom-0 top-[58px] h-auto w-[min(var(--workspace-inspector-width),100vw)] shadow-[-20px_0_50px_var(--color-shadow-0)] max-[700px]:w-screen min-[1001px]:static min-[1001px]:z-auto min-[1001px]:h-full min-[1001px]:w-auto min-[1001px]:shadow-none";
  return (
    <aside
      className={`inspector ${run ? "inspector-run" : "inspector-declaration"} z-30 flex min-h-0 min-w-0 flex-col overflow-hidden border-l border-line bg-panel ${panelLayout}`}
      aria-label={run ? "Run input" : "Workflow input declaration"}
    >
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-5 pt-[19px] pb-3.5">
        <div className="min-w-0 flex-1">
          <span className="eyebrow flex items-center gap-1.5 font-mono text-[9px] tracking-[.16em] text-amber uppercase">
            <SlidersHorizontal aria-hidden="true" className="size-3.5" strokeWidth={1.8} />
            Input · {run ? "Run" : definitionLabel}
          </span>
          <h2 className="mt-1 mb-0 min-w-0 text-lg [overflow-wrap:anywhere]">
            {"typeName" in inspected ? inspected.typeName : "Run input"}
          </h2>
          {run && triggeredBy && (
            <p className="mt-1 mb-0 text-[11px] text-secondary" data-run-trigger={triggeredBy}>
              {runTriggerLabel(triggeredBy)}
            </p>
          )}
          {cron && (
            <p className="mt-1 mb-0 text-[11px] text-secondary" data-cron={cron}>
              {scheduleNote}.
            </p>
          )}
        </div>
        <button
          type="button"
          className="icon-button grid size-[30px] shrink-0 cursor-pointer place-items-center rounded border border-line bg-panel p-0 text-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
          onClick={onClose}
          aria-label="Close"
        >
          <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
        </button>
      </header>
      <div className="inspector-body grid min-h-0 flex-1 content-start gap-4 overflow-auto p-5">
        {"error" in inspected ? (
          <p role="alert" className="m-0 text-[11px] text-danger [overflow-wrap:anywhere]">
            {inspected.error}
          </p>
        ) : (
          <>
            {inspected.values && (
              <section aria-label="Run values" className="min-w-0">
                <h3 className="inspector-section-title">Values used by this run</h3>
                <ul className="m-0 grid list-none gap-3 p-0">
                  {inspected.values.map((field) => (
                    <li
                      key={field.name}
                      className="min-w-0 text-[11px]"
                      data-value-source={field.source}
                    >
                      <div className="flex items-baseline gap-2">
                        <strong className="font-semibold">{field.name}</strong>
                        {field.source === "default" && (
                          <span className="text-[10px] text-muted italic">default</span>
                        )}
                      </div>
                      {field.source === "unrecorded" ? (
                        <p className="m-0 text-muted">
                          Excluded from serialization, so the run did not record it.
                        </p>
                      ) : (
                        // A value the run took from its default reads quieter than one supplied.
                        <div className={field.source === "default" ? "opacity-55" : undefined}>
                          <ValueView value={field.value} jsonOnly />
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {run && !inspected.values && (
              <p className="m-0 text-[11px] text-muted" data-run-values="unavailable">
                Input values are unavailable for this run.
              </p>
            )}
            {/* Fields and schema describe the definition; a run shows only its values. */}
            {!run && (
              <>
                <section aria-label="Input fields" className="min-w-0">
                  <h3 className="inspector-section-title">Fields</h3>
                  {inputDraft && !run ? (
                    <EditableFieldList fields={inspected.fields} draft={inputDraft} />
                  ) : (
                    <FieldList fields={inspected.fields} />
                  )}
                </section>
                <section aria-label="Input schema" className="min-w-0">
                  <h3 className="inspector-section-title">Schema</h3>
                  <DeclaredSchema label={inspected.typeName} schema={inspected.jsonSchema} />
                </section>
              </>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

function EditableFieldList({
  fields,
  draft,
}: {
  fields: WorkflowInputField[];
  draft: InputDraft;
}) {
  const editor = useInputFieldEditor(draft);
  return <FieldList fields={fields} editor={editor} />;
}

/** Each declared field; with an editor, a box to set the value Run and the schedule use. */
function FieldList({
  fields,
  editor,
}: {
  fields: WorkflowInputField[];
  editor?: InputFieldEditor;
}) {
  return (
    <>
      {editor?.error && (
        <p role="alert" className="m-0 mb-2 text-[11px] text-danger">
          {editor.error}
        </p>
      )}
      <ul className="m-0 grid list-none gap-3 p-0">
        {fields.map((field) => (
          <li key={field.name} className="min-w-0 text-[11px]">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <strong className="font-semibold">{field.name}</strong>
              <code className="font-mono text-[10px] text-muted">{field.typeLabel}</code>
              <span className="ml-auto">
                <FieldDefaultLabel field={field} />
              </span>
            </div>
            {editor && (
              <input
                type="text"
                aria-label={`${field.name} value`}
                className={`mt-1.5 w-full rounded border bg-panel px-2 py-1.5 font-mono text-[11px] text-ink outline-none focus:border-acid ${editor.invalid(field) ? "border-danger" : "border-line"}`}
                value={editor.text(field)}
                placeholder={field.default.kind === "computed" ? "computed per run" : ""}
                disabled={editor.error !== undefined}
                aria-invalid={editor.invalid(field) || undefined}
                onChange={(event) => editor.update(field, event.target.value)}
              />
            )}
            {field.description && (
              <div className="mt-1 text-secondary">
                <Markdown>{field.description}</Markdown>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}
