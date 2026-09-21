import { isUnknownRecord } from "./guards";

export type JsonValue =
  null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
type SchemaType = "null" | "boolean" | "object" | "array" | "number" | "integer" | "string";
export type Schema = boolean | JsonSchema;
export type JsonSchema = { [keyword: string]: JsonValue } & {
  type?: SchemaType | SchemaType[];
  title?: string;
  description?: string;
  $ref?: string;
  $defs?: Record<string, Schema>;
  definitions?: Record<string, Schema>;
  properties?: Record<string, Schema>;
  patternProperties?: Record<string, Schema>;
  dependentSchemas?: Record<string, Schema>;
  required?: string[];
  items?: Schema | Schema[];
  prefixItems?: Schema[];
  additionalProperties?: Schema;
  additionalItems?: Schema;
  unevaluatedProperties?: Schema;
  unevaluatedItems?: Schema;
  propertyNames?: Schema;
  contains?: Schema;
  anyOf?: Schema[];
  oneOf?: Schema[];
  allOf?: Schema[];
  not?: Schema;
  if?: Schema;
  then?: Schema;
  else?: Schema;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minContains?: number;
  maxContains?: number;
  minProperties?: number;
  maxProperties?: number;
  uniqueItems?: boolean;
  readOnly?: boolean;
  writeOnly?: boolean;
  deprecated?: boolean;
  format?: string;
  pattern?: string;
  enum?: JsonValue[];
  examples?: JsonValue[];
  dependentRequired?: Record<string, string[]>;
};

export interface StepOutput {
  type_name: string;
  json_schema: JsonSchema | null;
}

export interface StepInput extends StepOutput {
  name: string;
  required: boolean;
}

export interface StepInterface {
  step_inputs: StepInput[];
  step_output: StepOutput;
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!isUnknownRecord(value)) throw new Error(`${path} must be an object`);
  return value;
}

function fields(value: Record<string, unknown>, allowed: string[], path: string) {
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new Error(`${path}.${key} is not supported`);
  }
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${path} must be a non-empty string`);
  }
  return value;
}

function number(value: unknown, path: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    throw new Error(`${path} must be a finite number at least ${minimum}`);
  }
  return value;
}

function integer(value: unknown, path: string): number {
  const parsed = number(value, path);
  if (!Number.isSafeInteger(parsed)) throw new Error(`${path} must be a safe integer`);
  return parsed;
}

export function parseJsonValue(value: unknown, path: string): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value))
    return value.map((item: unknown, index: number) =>
      parseJsonValue(item, `${path}[${index}]`),
    );
  return Object.fromEntries(
    Object.entries(record(value, path)).map(([key, item]) => [
      key,
      parseJsonValue(item, `${path}.${key}`),
    ]),
  );
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${path} must be a boolean`);
  return value;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function schemaType(value: unknown, path: string): SchemaType {
  switch (value) {
    case "null":
    case "boolean":
    case "object":
    case "array":
    case "number":
    case "integer":
    case "string":
      return value;
    default:
      throw new Error(`${path} must be a JSON Schema type`);
  }
}

function schema(value: unknown, path: string): Schema {
  return typeof value === "boolean" ? value : schemaObject(value, path);
}

function schemaObject(value: unknown, path: string): JsonSchema {
  const parsed: JsonSchema = {};
  for (const [key, item] of Object.entries(record(value, path))) {
    const location = `${path}.${key}`;
    switch (key) {
      case "type":
        if (Array.isArray(item)) {
          if (!item.length) throw new Error(`${location} must not be empty`);
          parsed.type = item.map((value: unknown) => schemaType(value, location));
          if (new Set(parsed.type).size !== parsed.type.length)
            throw new Error(`${location} must contain unique types`);
        } else {
          parsed.type = schemaType(item, location);
        }
        break;
      case "$defs":
      case "definitions":
      case "properties":
      case "patternProperties":
      case "dependentSchemas":
        parsed[key] = Object.fromEntries(
          Object.entries(record(item, location)).map(([name, value]) => [
            name,
            schema(value, `${location}.${name}`),
          ]),
        );
        break;
      case "required":
        parsed.required = array(item, location).map((value) => {
          if (typeof value !== "string") throw new Error(`${location} must contain strings`);
          return value;
        });
        if (new Set(parsed.required).size !== parsed.required.length)
          throw new Error(`${location} must contain unique names`);
        break;
      case "dependentRequired":
        parsed.dependentRequired = Object.fromEntries(
          Object.entries(record(item, location)).map(([name, value]) => {
            const names = array(value, `${location}.${name}`).map((entry) => {
              if (typeof entry !== "string")
                throw new Error(`${location}.${name} must contain strings`);
              return entry;
            });
            if (new Set(names).size !== names.length)
              throw new Error(`${location}.${name} must contain unique names`);
            return [name, names];
          }),
        );
        break;
      case "items":
        parsed.items = Array.isArray(item)
          ? item.map((value: unknown, index: number) => schema(value, `${location}[${index}]`))
          : schema(item, location);
        break;
      case "prefixItems":
      case "anyOf":
      case "oneOf":
      case "allOf":
        parsed[key] = array(item, location).map((value, index) =>
          schema(value, `${location}[${index}]`),
        );
        if (!parsed[key].length) throw new Error(`${location} must not be empty`);
        break;
      case "additionalProperties":
      case "additionalItems":
      case "unevaluatedProperties":
      case "unevaluatedItems":
      case "propertyNames":
      case "contains":
      case "not":
      case "if":
      case "then":
      case "else":
        parsed[key] = schema(item, location);
        break;
      case "$ref":
      case "$dynamicRef":
      case "$schema":
      case "$id":
      case "$anchor":
      case "$dynamicAnchor":
      case "$comment":
      case "title":
      case "description":
      case "format":
      case "pattern":
      case "contentEncoding":
      case "contentMediaType":
        if (typeof item !== "string") throw new Error(`${location} must be a string`);
        parsed[key] = item;
        break;
      case "minimum":
      case "maximum":
      case "exclusiveMinimum":
      case "exclusiveMaximum":
        parsed[key] = number(item, location, -Infinity);
        break;
      case "multipleOf": {
        const divisor = number(item, location);
        if (divisor === 0) throw new Error(`${location} must be positive`);
        parsed[key] = divisor;
        break;
      }
      case "minLength":
      case "maxLength":
      case "minItems":
      case "maxItems":
      case "minContains":
      case "maxContains":
      case "minProperties":
      case "maxProperties":
        parsed[key] = integer(item, location);
        break;
      case "uniqueItems":
      case "readOnly":
      case "writeOnly":
      case "deprecated":
        parsed[key] = boolean(item, location);
        break;
      case "enum":
      case "examples":
        parsed[key] = array(item, location).map((value, index) =>
          parseJsonValue(value, `${location}[${index}]`),
        );
        if (key === "enum" && !parsed.enum?.length)
          throw new Error(`${location} must not be empty`);
        break;
      default:
        // JSON Schema has an open vocabulary, including Pydantic json_schema_extra.
        Object.defineProperty(parsed, key, {
          value: parseJsonValue(item, location),
          enumerable: true,
          configurable: true,
          writable: true,
        });
    }
  }
  return parsed;
}

export function parseNullableSchema(value: unknown, path: string): JsonSchema | null {
  return value === null ? null : schemaObject(value, path);
}

export function parseStepOutput(value: unknown, path: string): StepOutput {
  const item = record(value, path);
  fields(item, ["type_name", "json_schema"], path);
  return {
    type_name: string(item.type_name, `${path}.type_name`),
    json_schema: parseNullableSchema(item.json_schema, `${path}.json_schema`),
  };
}

export function parseStepInput(value: unknown, path: string): StepInput {
  const item = record(value, path);
  fields(item, ["name", "type_name", "json_schema", "required"], path);
  return {
    name: string(item.name, `${path}.name`),
    type_name: string(item.type_name, `${path}.type_name`),
    json_schema: parseNullableSchema(item.json_schema, `${path}.json_schema`),
    required: boolean(item.required, `${path}.required`),
  };
}

export function decodeStepInterface(raw: string): StepInterface {
  const path = "step interface";
  const value: unknown = JSON.parse(raw);
  const item = record(value, path);
  fields(item, ["step_inputs", "step_output"], path);
  return {
    step_inputs: array(item.step_inputs, `${path}.step_inputs`).map((value, index) =>
      parseStepInput(value, `${path}.step_inputs[${index}]`),
    ),
    step_output: parseStepOutput(item.step_output, `${path}.step_output`),
  };
}
