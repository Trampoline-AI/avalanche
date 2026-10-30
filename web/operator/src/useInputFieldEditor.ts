import { parseRunInput } from "./RunControls";
import type { InputDraft } from "./useInputDraft";
import { acceptsString, inputFieldText, type WorkflowInputField } from "./workflowInput";

export interface InputFieldEditor {
  /** Why the fields cannot be edited right now. */
  error?: string;
  text: (field: WorkflowInputField) => string;
  invalid: (field: WorkflowInputField) => boolean;
  update: (field: WorkflowInputField, text: string) => void;
}

/**
 * Edit one field of the shared input draft at a time. Text fields store what is
 * typed; other fields take JSON. An empty field leaves the field out of the input.
 */
export function useInputFieldEditor(draft: InputDraft): InputFieldEditor {
  const { pending, setPending } = draft;
  let values: Record<string, unknown> | undefined;
  let error: string | undefined;
  try {
    values = parseRunInput(draft.text);
  } catch {
    error = "The input draft is not a JSON object.";
  }
  return {
    error,
    text: (field) => pending[field.name] ?? inputFieldText(values?.[field.name], field),
    invalid: (field) => Object.hasOwn(pending, field.name),
    update: (field, text) => {
      if (!values) return;
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
    },
  };
}
