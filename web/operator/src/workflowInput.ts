import { isUnknownRecord } from "./guards";
import type { FlowInfoMsg, RunInputMsg, TriggerKind } from "./model";
import {
  parseJsonValue,
  parseNullableSchema,
  type JsonSchema,
  type JsonValue,
  type Schema,
} from "./stepInterface";

/** Graph node ID for the run input; the colon keeps it apart from Python node IDs. */
export const INPUT_NODE_ID = "ava:input";

export interface WorkflowInputDeclaration {
  typeName: string;
  jsonSchema: JsonSchema;
  consumerNodeIds: string[];
}

/**
 * How a field gets its value when the caller omits it. A "computed" default comes from
 * a `default_factory`, which the JSON schema cannot state, so each run computes it.
 */
export type FieldDefault =
  { kind: "required" } | { kind: "value"; value: JsonValue } | { kind: "computed" };

export interface WorkflowInputField {
  name: string;
  typeLabel: string;
  default: FieldDefault;
  description?: string;
  schema: Schema;
}

/**
 * Where a run's field value came from. "unrecorded" fields are excluded from
 * serialization, such as `Field(exclude=True)` secrets, so the run has no value to show.
 */
export type RunValueSource = "supplied" | "default" | "unrecorded";

/** One field's value in a run, and where that value came from. */
export interface RunInputField extends WorkflowInputField {
  /** Absent when the field is unrecorded. */
  value?: JsonValue;
  source: RunValueSource;
}

function stringList(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be a list of strings`);
  return value.map((item: unknown, index) => {
    if (typeof item !== "string") throw new Error(`${path}[${index}] must be a string`);
    return item;
  });
}

/** Validate the published input declaration; an empty value means no declared input. */
export function decodeWorkflowInputDeclaration(
  raw: string,
): WorkflowInputDeclaration | undefined {
  if (!raw) return undefined;
  const value: unknown = JSON.parse(raw);
  if (!isUnknownRecord(value)) throw new Error("input declaration must be an object");
  for (const key of Object.keys(value)) {
    if (!["type_name", "json_schema", "consumer_node_ids"].includes(key)) {
      throw new Error(`input declaration.${key} is not supported`);
    }
  }
  if (typeof value.type_name !== "string" || !value.type_name) {
    throw new Error("input declaration.type_name must be a non-empty string");
  }
  const jsonSchema = parseNullableSchema(value.json_schema, "input declaration.json_schema");
  if (jsonSchema === null) throw new Error("input declaration.json_schema must be an object");
  return {
    typeName: value.type_name,
    jsonSchema,
    consumerNodeIds: stringList(value.consumer_node_ids, "input declaration.consumer_node_ids"),
  };
}

function referenceName(reference: string): string {
  return reference.split("/").at(-1) || reference;
}

function typeLabel(schema: Schema): string {
  if (typeof schema === "boolean") return "any";
  if (schema.$ref !== undefined) return referenceName(schema.$ref);
  if (schema.enum) return schema.enum.map((item) => JSON.stringify(item)).join(" | ");
  if (schema.anyOf) return schema.anyOf.map(typeLabel).join(" | ");
  if (schema.type === "array" && schema.items !== undefined && !Array.isArray(schema.items)) {
    return `list[${typeLabel(schema.items)}]`;
  }
  if (Array.isArray(schema.type)) return schema.type.join(" | ");
  return schema.type ?? "any";
}

/** List the declared fields in schema order with their types, defaults and descriptions. */
export function workflowInputFields(
  declaration: WorkflowInputDeclaration,
): WorkflowInputField[] {
  const { jsonSchema } = declaration;
  return Object.entries(jsonSchema.properties ?? {}).map(([name, schema]) => {
    const property = typeof schema === "boolean" ? undefined : schema;
    const fieldDefault: FieldDefault =
      jsonSchema.required?.includes(name) === true
        ? { kind: "required" }
        : property !== undefined && property.default !== undefined
          ? { kind: "value", value: property.default }
          : { kind: "computed" };
    return {
      name,
      typeLabel: typeLabel(schema),
      default: fieldDefault,
      description: property?.description,
      schema,
    };
  });
}

/** Pair each declared field with the value the run used. */
export function runInputFields(
  declaration: WorkflowInputDeclaration,
  input: RunInputMsg,
): RunInputField[] {
  const values = parseJsonValue(JSON.parse(input.valuesJson), "run input");
  if (values === null || typeof values !== "object" || Array.isArray(values)) {
    throw new Error("run input must be an object");
  }
  const supplied = new Set(input.suppliedFields);
  return workflowInputFields(declaration).map((field) =>
    Object.hasOwn(values, field.name)
      ? {
          ...field,
          value: values[field.name],
          source: supplied.has(field.name) ? "supplied" : "default",
        }
      : { ...field, source: "unrecorded" },
  );
}

/** Short, single-line rendering of a value for graph cards and tables. */
export function formatInputValue(value: JsonValue): string {
  return JSON.stringify(value);
}

const TRIGGER_LABELS: Record<TriggerKind, string> = {
  manual: "Started manually",
  scheduled: "Started by its cron schedule",
  webhook: "Started by a webhook",
  api: "Started through the API",
};

/** Describe how a run started, from its summary's `triggeredBy`. */
export function runTriggerLabel(triggeredBy: TriggerKind): string {
  return TRIGGER_LABELS[triggeredBy];
}

/** What a workflow's cron runs start with, or undefined when it has no schedule. */
export function scheduledInputNote(workflow: FlowInfoMsg): string | undefined {
  if (!workflow.cron) return undefined;
  if (!workflow.scheduleEnabled) return `Scheduled runs (${workflow.cron}) are off`;
  return workflow.scheduleInput
    ? `Scheduled runs (${workflow.cron}) use the schedule's input`
    : `Scheduled runs (${workflow.cron}) use these defaults`;
}

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

/** Whether a field takes text, so the popup can store what is typed as a plain string. */
export function acceptsString(schema: Schema): boolean {
  if (typeof schema === "boolean") return schema;
  if (schema.enum) return schema.enum.every((item) => typeof item === "string");
  if (schema.anyOf) return schema.anyOf.some(acceptsString);
  if (Array.isArray(schema.type)) return schema.type.includes("string");
  return schema.type === "string";
}

/** How a draft value shows in the popup: text as typed, anything else as JSON, null as empty. */
export function inputFieldText(value: unknown, field: WorkflowInputField): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string" && acceptsString(field.schema)) return value;
  return JSON.stringify(value);
}
