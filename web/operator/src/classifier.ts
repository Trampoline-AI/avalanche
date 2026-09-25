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

export interface EvaluationDeclaration {
  metrics: Record<string, ClassifierQuestion>;
  composites: string[];
  runtime: ClassifierDeclaration["runtime"];
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
  const value: unknown = JSON.parse(raw);
  const path = "evaluation declaration";
  const item = record(value, path);
  fields(item, ["metrics", "composites", "runtime"], path);
  const metrics = Object.fromEntries(
    Object.entries(record(item.metrics, `${path}.metrics`)).map(([name, value]) => {
      string(name, `${path} metric name`);
      return [name, question(value, `${path}.metrics.${name}`)];
    }),
  );
  if (Object.keys(metrics).length === 0) throw new Error(`${path} needs at least one metric`);
  const composites = array(item.composites, `${path}.composites`).map((name, index) =>
    string(name, `${path}.composites[${index}]`),
  );
  if (new Set(composites).size !== composites.length)
    throw new Error(`${path}.composites must have unique names`);
  return { metrics, composites, runtime: declarationRuntime(item.runtime, `${path}.runtime`) };
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

function equalJson(left: JsonValue, right: JsonValue): boolean {
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

function answer(
  value: unknown,
  declared: ClassifierQuestion | undefined,
  path: string,
): ClassifierAnswer {
  const item = record(value, path);
  if (declared && item.type !== declared.type)
    throw new Error(`${path}.type does not match its question`);
  switch (item.type) {
    case "noul":
      fields(item, ["type", "noul"], path);
      return { type: "noul", noul: probability(item.noul, `${path}.noul`) };
    case "choice": {
      fields(item, ["type", "choice", "probabilities", "confidence"], path);
      const choice = string(item.choice, `${path}.choice`);
      const options =
        declared?.type === "choice"
          ? Object.keys(declared.criteria)
          : Object.keys(record(item.probabilities, `${path}.probabilities`));
      if (!options.length || options.some((option) => !option) || !options.includes(choice))
        throw new Error(`${path}.choice is not a valid option`);
      const distribution = probabilities(item.probabilities, options, `${path}.probabilities`);
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
      const criteria = declared?.type === "score" ? declared.criteria : undefined;
      const legend = entries(item.legend, `${path}.legend`);
      const levels = Array.from({ length: Object.keys(legend).length }, (_, index) =>
        String(index),
      );
      if (levels.length < 2 || Object.values(legend).some((level) => level === null))
        throw new Error(`${path}.legend needs at least two ordered levels`);
      matchingKeys(legend, levels, `${path}.legend`);
      if (criteria) {
        matchingKeys(
          legend,
          criteria.map((_, index) => String(index)),
          `${path}.legend`,
        );
        if (criteria.some((level, index) => !equalJson(level, legend[String(index)])))
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
    default:
      throw new Error(`${path}.type must be choice, noul, or score`);
  }
}

export function parseClassificationResult(
  value: unknown,
  questions?: Record<string, ClassifierQuestion>,
): ClassificationResult {
  const path = "classifier result";
  const item = record(value, path);
  fields(item, ["model", "answers", "usage"], path);
  const answers = record(item.answers, `${path}.answers`);
  if (questions) matchingKeys(answers, Object.keys(questions), `${path}.answers`);
  const usage = record(item.usage, `${path}.usage`);
  fields(usage, ["input_tokens", "output_tokens"], `${path}.usage`);
  return {
    model: string(item.model, `${path}.model`),
    answers: Object.fromEntries(
      Object.entries(answers).map(([id, value]) => {
        string(id, `${path} answer ID`);
        return [id, answer(value, questions?.[id], `${path}.answers.${id}`)];
      }),
    ),
    usage: {
      input_tokens:
        usage.input_tokens === null || usage.input_tokens === undefined
          ? null
          : integer(usage.input_tokens, `${path}.usage.input_tokens`),
      output_tokens:
        usage.output_tokens === null || usage.output_tokens === undefined
          ? null
          : integer(usage.output_tokens, `${path}.usage.output_tokens`),
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
  if (item.result !== null && declaration.questions === null)
    throw new Error("classifier result requires resolved questions");
  return {
    invocation_id: string(item.invocation_id, `${path}.invocation_id`),
    invocation_index: integer(item.invocation_index, `${path}.invocation_index`),
    status,
    started_at,
    ended_at,
    declaration,
    input,
    result:
      item.result === null
        ? null
        : parseClassificationResult(item.result, declaration.questions ?? undefined),
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
