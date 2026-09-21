import { isUnknownRecord } from "./guards";

export type ClassifierJson =
  null | boolean | number | string | ClassifierJson[] | { [key: string]: ClassifierJson };
export type ClassifierEntry =
  string | null | ClassifierJson[] | { [key: string]: ClassifierJson };

export type ClassifierQuestion =
  | { type: "choice"; instructions: ClassifierEntry; criteria: Record<string, ClassifierEntry> }
  | {
      type: "noul";
      instructions: ClassifierEntry;
      criteria: { true?: ClassifierEntry; false?: ClassifierEntry } | null;
    }
  | {
      type: "score";
      instructions: ClassifierEntry;
      criteria: Exclude<ClassifierEntry, null>[];
    };

type SchemaType = "null" | "boolean" | "object" | "array" | "number" | "integer" | "string";
export type ClassifierSchema = boolean | ClassifierJsonSchema;
export type ClassifierJsonSchema = { [keyword: string]: ClassifierJson } & {
  type?: SchemaType | SchemaType[];
  title?: string;
  description?: string;
  $ref?: string;
  $defs?: Record<string, ClassifierSchema>;
  definitions?: Record<string, ClassifierSchema>;
  properties?: Record<string, ClassifierSchema>;
  patternProperties?: Record<string, ClassifierSchema>;
  dependentSchemas?: Record<string, ClassifierSchema>;
  required?: string[];
  items?: ClassifierSchema | ClassifierSchema[];
  prefixItems?: ClassifierSchema[];
  additionalProperties?: ClassifierSchema;
  additionalItems?: ClassifierSchema;
  unevaluatedProperties?: ClassifierSchema;
  unevaluatedItems?: ClassifierSchema;
  propertyNames?: ClassifierSchema;
  contains?: ClassifierSchema;
  anyOf?: ClassifierSchema[];
  oneOf?: ClassifierSchema[];
  allOf?: ClassifierSchema[];
  not?: ClassifierSchema;
  if?: ClassifierSchema;
  then?: ClassifierSchema;
  else?: ClassifierSchema;
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
  enum?: ClassifierJson[];
  examples?: ClassifierJson[];
  dependentRequired?: Record<string, string[]>;
};

export interface ClassifierStepOutput {
  type_name: string;
  json_schema: ClassifierJsonSchema | null;
}

export interface ClassifierStepInput extends ClassifierStepOutput {
  name: string;
  required: boolean;
}

export interface ClassifierDeclaration {
  questions: Record<string, ClassifierQuestion>;
  runtime: { model: string; timeout: number };
  input_schema: ClassifierJsonSchema | null;
  step_inputs: ClassifierStepInput[];
  step_output: ClassifierStepOutput;
}

export type ClassifierAnswer =
  | {
      type: "choice";
      choice: string;
      probabilities: Record<string, number>;
      confidence: number;
    }
  | { type: "noul"; noul: number }
  | {
      type: "score";
      score: number;
      legend: Record<string, ClassifierEntry>;
      probabilities: Record<string, number>;
      confidence: number;
    };

export interface ClassificationResult {
  model: string;
  answers: Record<string, ClassifierAnswer>;
  usage: { input_tokens: number; output_tokens: number };
}

export interface ClassifierInvocation {
  invocation_id: string;
  invocation_index: number;
  status: "running" | "success" | "failed" | "cancelled";
  started_at: number;
  ended_at: number | null;
  declaration: ClassifierDeclaration;
  input: ClassifierEntry;
  result: ClassificationResult | null;
  error: string | null;
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

function probability(value: unknown, path: string): number {
  const parsed = number(value, path);
  if (parsed > 1) throw new Error(`${path} must be at most 1`);
  return parsed;
}

function json(value: unknown, path: string): ClassifierJson {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value))
    return value.map((item: unknown, index: number) => json(item, `${path}[${index}]`));
  return Object.fromEntries(
    Object.entries(record(value, path)).map(([key, item]) => [
      key,
      json(item, `${path}.${key}`),
    ]),
  );
}

function entry(value: unknown, path: string): ClassifierEntry {
  const parsed = json(value, path);
  if (typeof parsed === "boolean" || typeof parsed === "number") {
    throw new Error(`${path} must be text, an object, an array, or null`);
  }
  return parsed;
}

function entries(value: unknown, path: string): Record<string, ClassifierEntry> {
  return Object.fromEntries(
    Object.entries(record(value, path)).map(([key, item]) => [
      key,
      entry(item, `${path}.${key}`),
    ]),
  );
}

function question(value: unknown, path: string): ClassifierQuestion {
  const item = record(value, path);
  fields(item, ["type", "instructions", "criteria"], path);
  const instructions = entry(item.instructions, `${path}.instructions`);
  switch (item.type) {
    case "choice": {
      const criteria = entries(item.criteria, `${path}.criteria`);
      if (Object.keys(criteria).length === 0)
        throw new Error(`${path}.criteria needs at least one option`);
      for (const key of Object.keys(criteria)) string(key, `${path}.criteria option`);
      return { type: "choice", instructions, criteria };
    }
    case "noul": {
      if (item.criteria === null) return { type: "noul", instructions, criteria: null };
      const criteria = entries(item.criteria, `${path}.criteria`);
      fields(criteria, ["true", "false"], `${path}.criteria`);
      return { type: "noul", instructions, criteria };
    }
    case "score": {
      if (!Array.isArray(item.criteria) || item.criteria.length < 2) {
        throw new Error(`${path}.criteria needs at least two ordered levels`);
      }
      const criteria = item.criteria.map((level: unknown, index: number) => {
        const parsed = entry(level, `${path}.criteria[${index}]`);
        if (parsed === null)
          throw new Error(`${path}.criteria[${index}] must describe its level`);
        return parsed;
      });
      return { type: "score", instructions, criteria };
    }
    default:
      throw new Error(`${path}.type must be choice, noul, or score`);
  }
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

function schema(value: unknown, path: string): ClassifierSchema {
  return typeof value === "boolean" ? value : schemaObject(value, path);
}

function schemaObject(value: unknown, path: string): ClassifierJsonSchema {
  const parsed: ClassifierJsonSchema = {};
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
          json(value, `${location}[${index}]`),
        );
        if (key === "enum" && !parsed.enum?.length)
          throw new Error(`${location} must not be empty`);
        break;
      default:
        // JSON Schema has an open vocabulary, including Pydantic json_schema_extra.
        Object.defineProperty(parsed, key, {
          value: json(item, location),
          enumerable: true,
          configurable: true,
          writable: true,
        });
    }
  }
  return parsed;
}

function nullableSchema(value: unknown, path: string): ClassifierJsonSchema | null {
  return value === null ? null : schemaObject(value, path);
}

function stepOutput(value: unknown, path: string): ClassifierStepOutput {
  const item = record(value, path);
  fields(item, ["type_name", "json_schema"], path);
  return {
    type_name: string(item.type_name, `${path}.type_name`),
    json_schema: nullableSchema(item.json_schema, `${path}.json_schema`),
  };
}

function stepInput(value: unknown, path: string): ClassifierStepInput {
  const item = record(value, path);
  fields(item, ["name", "type_name", "json_schema", "required"], path);
  return {
    name: string(item.name, `${path}.name`),
    type_name: string(item.type_name, `${path}.type_name`),
    json_schema: nullableSchema(item.json_schema, `${path}.json_schema`),
    required: boolean(item.required, `${path}.required`),
  };
}

export function parseClassifierDeclaration(value: unknown): ClassifierDeclaration {
  const path = "classifier declaration";
  const item = record(value, path);
  fields(item, ["questions", "runtime", "input_schema", "step_inputs", "step_output"], path);
  const questions = Object.fromEntries(
    Object.entries(record(item.questions, `${path}.questions`)).map(([id, value]) => {
      string(id, `${path} question ID`);
      return [id, question(value, `${path}.questions.${id}`)];
    }),
  );
  if (Object.keys(questions).length === 0)
    throw new Error(`${path} needs at least one question`);
  const runtime = record(item.runtime, `${path}.runtime`);
  fields(runtime, ["model", "timeout"], `${path}.runtime`);
  const timeout = number(runtime.timeout, `${path}.runtime.timeout`);
  if (timeout === 0) throw new Error(`${path}.runtime.timeout must be positive`);
  return {
    questions,
    runtime: { model: string(runtime.model, `${path}.runtime.model`), timeout },
    input_schema: nullableSchema(item.input_schema, `${path}.input_schema`),
    step_inputs: array(item.step_inputs, `${path}.step_inputs`).map((value, index) =>
      stepInput(value, `${path}.step_inputs[${index}]`),
    ),
    step_output: stepOutput(item.step_output, `${path}.step_output`),
  };
}

function matchingKeys(value: Record<string, unknown>, expected: string[], path: string) {
  if (
    Object.keys(value).length !== expected.length ||
    expected.some((key) => !Object.hasOwn(value, key))
  ) {
    throw new Error(`${path} must match the declared keys`);
  }
}

function numericallyEqual(left: number, right: number): boolean {
  // Match the classifier domain model's relative and absolute tolerance.
  return (
    Math.abs(left - right) <= Math.max(1e-5, 1e-5 * Math.max(Math.abs(left), Math.abs(right)))
  );
}

function probabilities(value: unknown, keys: string[], path: string): Record<string, number> {
  const item = record(value, path);
  matchingKeys(item, keys, path);
  let total = 0;
  const parsed = Object.fromEntries(
    keys.map((key) => {
      const parsed = probability(item[key], `${path}.${key}`);
      total += parsed;
      return [key, parsed];
    }),
  );
  // Match the API's independently rounded, two-decimal probabilities.
  if (total <= 0 || Math.abs(total - 1) > 0.005 * keys.length + 1e-12)
    throw new Error(`${path} must sum to 1 within rounding precision`);
  return parsed;
}

function equalJson(left: ClassifierJson, right: ClassifierJson): boolean {
  if (left === right) return true;
  if (Array.isArray(left)) {
    return (
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => equalJson(value, right[index]))
    );
  }
  if (
    typeof left === "object" &&
    left !== null &&
    typeof right === "object" &&
    right !== null &&
    !Array.isArray(right)
  ) {
    return (
      Object.keys(left).length === Object.keys(right).length &&
      Object.entries(left).every(
        ([key, value]) => Object.hasOwn(right, key) && equalJson(value, right[key]),
      )
    );
  }
  return false;
}

function answer(value: unknown, declared: ClassifierQuestion, path: string): ClassifierAnswer {
  const item = record(value, path);
  if (item.type !== declared.type) throw new Error(`${path}.type does not match its question`);
  switch (declared.type) {
    case "noul":
      fields(item, ["type", "noul"], path);
      return { type: "noul", noul: probability(item.noul, `${path}.noul`) };
    case "choice": {
      fields(item, ["type", "choice", "probabilities", "confidence"], path);
      const choice = string(item.choice, `${path}.choice`);
      if (!Object.hasOwn(declared.criteria, choice))
        throw new Error(`${path}.choice is not a declared option`);
      const distribution = probabilities(
        item.probabilities,
        Object.keys(declared.criteria),
        `${path}.probabilities`,
      );
      const maximum = Object.values(distribution).reduce(
        (maximum, value) => Math.max(maximum, value),
        0,
      );
      if (!numericallyEqual(distribution[choice], maximum))
        throw new Error(`${path}.choice must have maximum probability`);
      return {
        type: "choice",
        choice,
        probabilities: distribution,
        confidence: probability(item.confidence, `${path}.confidence`),
      };
    }
    case "score": {
      fields(item, ["type", "score", "legend", "probabilities", "confidence"], path);
      const levels = declared.criteria.map((_, index) => String(index));
      const legend = entries(item.legend, `${path}.legend`);
      matchingKeys(legend, levels, `${path}.legend`);
      if (declared.criteria.some((level, index) => !equalJson(level, legend[String(index)]))) {
        throw new Error(`${path}.legend does not match its declared levels`);
      }
      const score = number(item.score, `${path}.score`);
      if (score > levels.length - 1)
        throw new Error(`${path}.score is outside its declared levels`);
      const distribution = probabilities(item.probabilities, levels, `${path}.probabilities`);
      const expected = levels.reduce(
        (sum, level) => sum + Number(level) * distribution[level],
        0,
      );
      if (!numericallyEqual(score, expected))
        throw new Error(`${path}.score must match its probability-weighted levels`);
      return {
        type: "score",
        score,
        legend,
        probabilities: distribution,
        confidence: probability(item.confidence, `${path}.confidence`),
      };
    }
  }
}

function result(value: unknown, declaration: ClassifierDeclaration): ClassificationResult {
  const path = "classifier result";
  const item = record(value, path);
  fields(item, ["model", "answers", "usage"], path);
  const answers = record(item.answers, `${path}.answers`);
  matchingKeys(answers, Object.keys(declaration.questions), `${path}.answers`);
  const usage = record(item.usage, `${path}.usage`);
  fields(usage, ["input_tokens", "output_tokens"], `${path}.usage`);
  return {
    model: string(item.model, `${path}.model`),
    answers: Object.fromEntries(
      Object.entries(declaration.questions).map(([id, declared]) => [
        id,
        answer(answers[id], declared, `${path}.answers.${id}`),
      ]),
    ),
    usage: {
      input_tokens: integer(usage.input_tokens, `${path}.usage.input_tokens`),
      output_tokens: integer(usage.output_tokens, `${path}.usage.output_tokens`),
    },
  };
}

export function parseClassifierInvocation(value: unknown): ClassifierInvocation {
  const path = "classifier invocation";
  const item = record(value, path);
  fields(
    item,
    [
      "invocation_id",
      "invocation_index",
      "status",
      "started_at",
      "ended_at",
      "declaration",
      "input",
      "result",
      "error",
    ],
    path,
  );
  const input = entry(item.input, `${path}.input`);
  const declaration = parseClassifierDeclaration(item.declaration);
  const status = item.status;
  if (
    status !== "running" &&
    status !== "success" &&
    status !== "failed" &&
    status !== "cancelled"
  ) {
    throw new Error(`${path}.status is unsupported`);
  }
  const started_at = number(item.started_at, `${path}.started_at`);
  const ended_at =
    item.ended_at === null ? null : number(item.ended_at, `${path}.ended_at`, started_at);
  const error = item.error === null ? null : string(item.error, `${path}.error`);
  if (status === "running" ? ended_at !== null : ended_at === null)
    throw new Error(`${path} has inconsistent lifecycle timestamps`);
  if (status === "success" ? item.result === null : item.result !== null)
    throw new Error(`${path} has an inconsistent result`);
  if ((status === "running" || status === "success") && error !== null)
    throw new Error(`${path} has an unexpected error`);
  if (status === "failed" && error === null)
    throw new Error(`${path} is missing its failure error`);
  return {
    invocation_id: string(item.invocation_id, `${path}.invocation_id`),
    invocation_index: integer(item.invocation_index, `${path}.invocation_index`),
    status,
    started_at,
    ended_at,
    declaration,
    input,
    result: item.result === null ? null : result(item.result, declaration),
    error,
  };
}

export function decodeClassifierDeclaration(raw: string): ClassifierDeclaration {
  const value: unknown = JSON.parse(raw);
  return parseClassifierDeclaration(value);
}

export function decodeClassifierInvocation(raw: string): ClassifierInvocation {
  const value: unknown = JSON.parse(raw);
  return parseClassifierInvocation(value);
}
