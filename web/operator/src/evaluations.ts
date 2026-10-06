import type { ClassificationResult } from "./classifier";
import type { EvaluationRecordV2 } from "./generated/operator";

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

export function mapEvaluationRecord(record: EvaluationRecordV2): EvaluationRecord {
  const identity = {
    evaluationId: record.evaluationId,
    runId: record.runId,
    nodeId: record.nodeId,
    createdAt: record.createdAt,
  };
  switch (record.status) {
    case "pending":
      return { ...identity, status: "pending" };
    case "completed": {
      const result: EvaluationResult = JSON.parse(record.resultJson!);
      return { ...identity, status: "completed", endedAt: record.endedAt!, result };
    }
    case "failed":
      return {
        ...identity,
        status: "failed",
        endedAt: record.endedAt!,
        error: record.error!,
      };
    default:
      throw new Error(`Unsupported evaluation status: ${record.status}`);
  }
}

const compositePercent = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

export function formatCompositeSummary(composites: Record<string, number>): string {
  const entries = Object.entries(composites);
  if (entries.length === 1) {
    const [name, value] = entries[0];
    return `${name}: ${compositePercent.format(value)}`;
  }
  return entries.map(([, value]) => compositePercent.format(value)).join(" · ");
}
