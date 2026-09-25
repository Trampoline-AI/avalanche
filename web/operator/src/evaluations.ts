import { parseClassificationResult, type ClassificationResult } from "./classifier";
import { isUnknownRecord } from "./guards";

export interface EvaluationResult {
  classification: ClassificationResult;
  composites: Record<string, number>;
}

interface EvaluationIdentity {
  evaluationId: string;
  runId: string;
  nodeId: string;
  createdAt: number;
}

export type EvaluationRecord = EvaluationIdentity &
  (
    | { status: "pending"; endedAt?: never; result?: never; error?: never }
    | { status: "completed"; endedAt: number; result: EvaluationResult; error?: never }
    | { status: "failed"; endedAt: number; result?: never; error: string }
  );

function text(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.length)
    throw new Error(`Evaluation ${field} must be a non-empty string`);
  return value;
}

function timestamp(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("Evaluation timestamp must be finite");
  return value;
}

function parseResult(raw: unknown): EvaluationResult {
  const value: unknown = JSON.parse(text(raw, "result"));
  if (
    !isUnknownRecord(value) ||
    !isUnknownRecord(value.composites) ||
    Object.keys(value).some((key) => key !== "classification" && key !== "composites")
  )
    throw new Error("Invalid evaluation result");
  const composites = Object.fromEntries(
    Object.entries(value.composites).map(([name, score]) => {
      text(name, "composite name");
      if (typeof score !== "number" || !Number.isFinite(score) || score < 0 || score > 1)
        throw new Error(`Evaluation composite ${name} must be between 0 and 1`);
      return [name, score];
    }),
  );
  return { classification: parseClassificationResult(value.classification), composites };
}

export function parseEvaluationRecords(
  value: unknown,
  runId: string,
  nodeId: string,
): EvaluationRecord[] {
  if (!Array.isArray(value)) throw new Error("Evaluation records must be an array");
  const ids = new Set<string>();
  return value.map((item: unknown): EvaluationRecord => {
    if (!isUnknownRecord(item)) throw new Error("Invalid evaluation record");
    const identity = {
      evaluationId: text(item.evaluationId, "ID"),
      runId: text(item.runId, "run ID"),
      nodeId: text(item.nodeId, "node ID"),
      createdAt: timestamp(item.createdAt),
    };
    if (identity.runId !== runId || identity.nodeId !== nodeId)
      throw new Error("Evaluation belongs to another execution selection");
    if (ids.has(identity.evaluationId)) throw new Error("Duplicate evaluation ID");
    ids.add(identity.evaluationId);
    if (item.status === "pending") {
      if (
        item.endedAt !== undefined ||
        item.resultJson !== undefined ||
        item.error !== undefined
      )
        throw new Error("Pending evaluation contains terminal evidence");
      return { ...identity, status: "pending" };
    }
    const endedAt = timestamp(item.endedAt);
    if (endedAt < identity.createdAt) throw new Error("Evaluation ends before it starts");
    if (item.status === "completed" && item.error === undefined)
      return {
        ...identity,
        status: "completed",
        endedAt,
        result: parseResult(item.resultJson),
      };
    if (item.status === "failed" && item.resultJson === undefined)
      return { ...identity, status: "failed", endedAt, error: text(item.error, "error") };
    throw new Error("Invalid evaluation lifecycle");
  });
}
