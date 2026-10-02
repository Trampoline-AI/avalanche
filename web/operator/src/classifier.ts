import { isUnknownRecord } from "./guards";
import {
  type JsonSchema,
  type JsonValue,
  type StepInterface,
  parseJsonValue,
  parseNullableSchema,
  parseStepInput,
  parseStepOutput,
} from "./stepInterface";

export type ClassifierEntry = string | null | JsonValue[] | { [key: string]: JsonValue };

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

export interface ClassifierDeclaration extends StepInterface {
  questions: Record<string, ClassifierQuestion> | null;
  runtime: { model: string; timeout: number };
  input_schema: JsonSchema | null;
}

export interface MetricInput {
  source: "trace" | "output" | "input" | "custom";
  selector: string;
}

export interface EvaluationDeclaration {
  metrics: Record<string, ClassifierQuestion>;
  composites: string[];
  runtime: ClassifierDeclaration["runtime"];
  metric_inputs?: Record<string, MetricInput[]>;
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
  usage: { input_tokens: number | null; output_tokens: number | null };
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

function entry(value: unknown, path: string): ClassifierEntry {
  const parsed = parseJsonValue(value, path);
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

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function declarationRuntime(value: unknown, path: string): ClassifierDeclaration["runtime"] {
  const runtime = record(value, path);
  fields(runtime, ["model", "timeout"], path);
  const timeout = number(runtime.timeout, `${path}.timeout`);
  if (timeout === 0) throw new Error(`${path}.timeout must be positive`);
  return { model: string(runtime.model, `${path}.model`), timeout };
}

export function decodeEvaluationDeclaration(raw: string): EvaluationDeclaration {
  return JSON.parse(raw);
}

export function parseClassifierDeclaration(value: unknown): ClassifierDeclaration {
  const path = "classifier declaration";
  const item = record(value, path);
  fields(item, ["questions", "runtime", "input_schema", "step_inputs", "step_output"], path);
  const questions =
    item.questions === null
      ? null
      : Object.fromEntries(
          Object.entries(record(item.questions, `${path}.questions`)).map(([id, value]) => {
            string(id, `${path} question ID`);
            return [id, question(value, `${path}.questions.${id}`)];
          }),
        );
  if (questions !== null && Object.keys(questions).length === 0)
    throw new Error(`${path} needs at least one question`);
  return {
    questions,
    runtime: declarationRuntime(item.runtime, `${path}.runtime`),
    input_schema: parseNullableSchema(item.input_schema, `${path}.input_schema`),
    step_inputs: array(item.step_inputs, `${path}.step_inputs`).map((value, index) =>
      parseStepInput(value, `${path}.step_inputs[${index}]`),
    ),
    step_output: parseStepOutput(item.step_output, `${path}.step_output`),
  };
}

export function decodeClassifierDeclaration(raw: string): ClassifierDeclaration {
  const value: unknown = JSON.parse(raw);
  return parseClassifierDeclaration(value);
}

export function decodeClassifierInvocation(raw: string): ClassifierInvocation {
  return JSON.parse(raw);
}
