import { isUnknownRecord } from "./guards";
import type { RunInputMsg, RunTrigger } from "./model";
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

const TRIGGER_LABELS: Record<RunTrigger, string> = {
  manual: "Started manually",
  scheduled: "Started by its cron schedule",
  webhook: "Started by a webhook",
};

/** Describe how a run started. */
export function runTriggerLabel(triggeredBy: RunTrigger): string {
  return TRIGGER_LABELS[triggeredBy];
}
