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

export interface ClassifierDeclaration {
  questions: Record<string, ClassifierQuestion>;
  runtime: { model: string; timeout: number };
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

export function parseClassifierDeclaration(value: unknown): ClassifierDeclaration {
  const path = "classifier declaration";
  const item = record(value, path);
  fields(item, ["questions", "runtime"], path);
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
