import { useMemo } from "react";
import { SlidersHorizontal, X } from "lucide-react";

import type { FlowInfoMsg, RunSnapshotMsg } from "./model";
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
  type RunValueSource,
  type WorkflowInputField,
} from "./workflowInput";

interface WorkflowInputInspectorProps {
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  embedded?: boolean;
  definitionLabel?: string;
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

const VALUE_SOURCE_LABELS: Record<RunValueSource, string> = {
  supplied: "Supplied",
  default: "Default",
  unrecorded: "Not recorded",
};

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
                        <span
                          className={`ml-auto shrink-0 rounded px-1.5 py-0.5 font-mono text-[9px] uppercase ${field.source === "supplied" ? "bg-acid/10 text-acid" : "bg-canvas text-muted"}`}
                        >
                          {VALUE_SOURCE_LABELS[field.source]}
                        </span>
                      </div>
                      {field.source === "unrecorded" ? (
                        <p className="m-0 text-muted">
                          Excluded from serialization, so the run did not record it.
                        </p>
                      ) : (
                        <ValueView value={field.value} jsonOnly />
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
            <section aria-label="Input fields" className="min-w-0">
              <h3 className="inspector-section-title">Fields</h3>
              <ul className="m-0 grid list-none gap-3 p-0">
                {inspected.fields.map((field) => (
                  <li key={field.name} className="min-w-0 text-[11px]">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                      <strong className="font-semibold">{field.name}</strong>
                      <code className="font-mono text-[10px] text-muted">
                        {field.typeLabel}
                      </code>
                      <span className="ml-auto">
                        <FieldDefaultLabel field={field} />
                      </span>
                    </div>
                    {field.description && (
                      <div className="mt-1 text-secondary">
                        <Markdown>{field.description}</Markdown>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </section>
            <section aria-label="Input schema" className="min-w-0">
              <h3 className="inspector-section-title">Schema</h3>
              <DeclaredSchema label={inspected.typeName} schema={inspected.jsonSchema} />
            </section>
          </>
        )}
      </div>
    </aside>
  );
}
