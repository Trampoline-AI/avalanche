import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { parseRunInput } from "./RunControls";
import type { InputDraft } from "./useInputDraft";
import {
  acceptsString,
  formatInputValue,
  inputFieldText,
  type RunInputField,
  type WorkflowInputField,
} from "./workflowInput";

interface InputPopoverProps {
  typeName?: string;
  fields: WorkflowInputField[];
  /** Editable: the draft Run and the schedule start with. */
  draft?: InputDraft;
  /** Read-only: the values a run used, or "unavailable" when it has none to show. */
  runValues?: RunInputField[] | "unavailable";
  note?: string;
  error?: string;
  onClose: () => void;
}

const SOURCE_TAGS: Record<RunInputField["source"], string> = {
  supplied: "",
  default: "default",
  unrecorded: "not recorded",
};

/** The run input's fields over the graph's input dot, editable outside a run. */
export function InputPopover({
  typeName,
  fields,
  draft,
  runValues,
  note,
  error,
  onClose,
}: InputPopoverProps) {
  const element = useRef<HTMLDivElement>(null);
  // Text that does not parse yet, kept per field until it does.
  const [pending, setPending] = useState<Record<string, string>>({});
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (!element.current?.contains(event.target as Node)) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  let values: Record<string, unknown> | undefined;
  let draftError: string | undefined;
  if (draft) {
    try {
      values = parseRunInput(draft.text);
    } catch {
      draftError = "The JSON input has an error; fix it in the run controls.";
    }
  } else {
    // Read-only outside a run: show the declared defaults.
    values = Object.fromEntries(
      fields.flatMap((field) =>
        field.default.kind === "value" ? [[field.name, field.default.value]] : [],
      ),
    );
  }

  const update = (field: WorkflowInputField, text: string) => {
    if (!draft || !values) return;
    const next = { ...values };
    if (text.trim() === "") {
      delete next[field.name];
    } else if (acceptsString(field.schema)) {
      next[field.name] = text;
    } else {
      try {
        next[field.name] = JSON.parse(text);
      } catch {
        setPending((current) => ({ ...current, [field.name]: text }));
        return;
      }
    }
    setPending(({ [field.name]: _, ...rest }) => rest);
    draft.setText(JSON.stringify(next, null, 2));
  };

  return (
    <div
      ref={element}
      role="dialog"
      aria-label={`Workflow input${typeName ? ` ${typeName}` : ""}`}
      className="input-popover nodrag nowheel nopan w-[320px] cursor-default rounded border border-line-strong bg-panel text-left shadow-[0_18px_50px_var(--color-shadow-1)]"
    >
      <header className="flex items-start justify-between gap-2 border-b border-line px-3.5 pt-3 pb-2.5">
        <div className="min-w-0">
          <strong className="block text-[12px] text-ink">Inputs</strong>
          {typeName && (
            <span className="block truncate font-mono text-[10px] text-secondary">
              {typeName}
            </span>
          )}
          {note && <p className="m-0 mt-1 text-[10px] text-secondary">{note}</p>}
        </div>
        <button
          type="button"
          className="grid size-6 shrink-0 cursor-pointer place-items-center rounded border-0 bg-transparent p-0 text-secondary hover:text-ink"
          aria-label="Close inputs"
          onClick={onClose}
        >
          <X aria-hidden="true" className="size-3.5" />
        </button>
      </header>
      <div className="max-h-[360px] overflow-y-auto px-3.5 py-2.5">
        {error ? (
          <p className="m-0 text-[11px] text-danger">{error}</p>
        ) : runValues === "unavailable" ? (
          <p className="m-0 text-[11px] text-muted">This run's input values are unavailable.</p>
        ) : runValues ? (
          <dl className="m-0 grid grid-cols-[minmax(0,auto)_minmax(0,1fr)_auto] items-baseline gap-x-2 gap-y-1.5 font-mono text-[11px]">
            {runValues.map((field) => (
              <div key={field.name} className="contents" data-value-source={field.source}>
                <dt className="truncate text-ink">{field.name}</dt>
                <dd
                  className="m-0 truncate text-secondary"
                  title={formatInputValue(field.value ?? null)}
                >
                  {field.value === undefined ? "" : formatInputValue(field.value)}
                </dd>
                <dd className="m-0 text-[9px] tracking-[.08em] text-muted uppercase">
                  {SOURCE_TAGS[field.source]}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="flex flex-col gap-2.5">
            {draftError && <p className="m-0 text-[11px] text-danger">{draftError}</p>}
            {fields.map((field) => {
              const text = pending[field.name] ?? inputFieldText(values?.[field.name], field);
              const invalid = Object.hasOwn(pending, field.name);
              return (
                <label key={field.name} className="flex flex-col gap-1">
                  <span className="flex items-baseline gap-1.5 font-mono text-[11px]">
                    <span className="text-ink">{field.name}</span>
                    <span className="truncate text-[10px] text-muted">{field.typeLabel}</span>
                    {field.default.kind === "required" && (
                      <span className="ml-auto text-[9px] tracking-[.08em] text-amber uppercase">
                        required
                      </span>
                    )}
                  </span>
                  <input
                    type="text"
                    className={`rounded border bg-panel px-2 py-1 font-mono text-[11px] text-ink outline-none focus:border-acid ${invalid ? "border-danger" : "border-line"}`}
                    value={text}
                    placeholder={field.default.kind === "computed" ? "computed per run" : ""}
                    disabled={draft === undefined || values === undefined}
                    aria-invalid={invalid || undefined}
                    title={field.description}
                    onChange={(event) => update(field, event.target.value)}
                  />
                </label>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
