import { isUnknownRecord } from "./guards";
import type { FlowInfoMsg } from "./model";

/**
 * Starting input for a workflow's runs, or undefined when the workflow declares
 * no input fields. The input its schedule already runs with wins; other fields
 * take their declared default, and required fields without one are left as null
 * for the operator to fill in.
 */
export function workflowInputDraft(workflow: FlowInfoMsg): Record<string, unknown> | undefined {
  if (!workflow.inputSchemaJson) return undefined;
  const schema: unknown = JSON.parse(workflow.inputSchemaJson);
  if (!isUnknownRecord(schema)) throw new Error("Workflow input schema must be a JSON object");
  const properties = schema.properties;
  if (!isUnknownRecord(properties) || Object.keys(properties).length === 0) return undefined;
  const required = Array.isArray(schema.required) ? schema.required : [];
  const draft: Record<string, unknown> = {};
  for (const [name, field] of Object.entries(properties)) {
    if (isUnknownRecord(field) && "default" in field) draft[name] = field.default;
    else if (required.includes(name)) draft[name] = null;
  }
  return { ...draft, ...workflow.scheduleInput };
}
