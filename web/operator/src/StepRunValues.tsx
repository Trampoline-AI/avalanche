import { useCallback } from "react";

import type { OperatorApi } from "./api";
import { useRead } from "./MachineWorkspace";
import type { JsonValue } from "./machine";
import type { NodeSnapshotMsg } from "./model";
import type { StepValuePreview } from "./stepIo";
import { ValueView } from "./ValueView";

/** A step's accepted typed result, read from the host's shared result route. */
export type StepResultRead =
  | { status: "loading" }
  | { status: "ready"; value: JsonValue }
  | { status: "unavailable"; error: string };

/**
 * Read the accepted result of a finished step: a flow run's step, or (with
 * `visit`) a machine visit's step, whose snapshot node ID is visit-qualified
 * (`v<visit>.<step>`) while the result route takes the step's own ID.
 */
export function useStepResult(
  api: OperatorApi,
  runId: string | undefined,
  node: NodeSnapshotMsg | undefined,
  visit?: number,
): StepResultRead | undefined {
  const nodeId = node?.nodeId;
  const stepId =
    nodeId && visit !== undefined && nodeId.startsWith(`v${visit}.`)
      ? nodeId.slice(`v${visit}.`.length)
      : nodeId;
  const load = useCallback(
    (signal: AbortSignal) => api.getRunResult!(runId!, { visit, nodeId: stepId }, signal),
    [api, runId, visit, stepId],
  );
  const ready = Boolean(api.getRunResult && runId && stepId && node?.status === "success");
  const read = useRead(ready ? load : undefined);
  if (!ready) return undefined;
  if (read.error !== undefined) return { status: "unavailable", error: read.error };
  return read.value === undefined
    ? { status: "loading" }
    : { status: "ready", value: read.value };
}

function StepValue({ label, preview }: { label: string; preview: StepValuePreview }) {
  return (
    <div className="step-run-value min-w-0" role="group" aria-label={label}>
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <strong className="text-[11px] font-semibold text-ink">{label}</strong>
        <code className="min-w-0 flex-1 font-mono text-[10px] text-muted [overflow-wrap:anywhere]">
          {preview.typeName}
        </code>
        {preview.truncated && (
          <span
            className="rounded border border-line px-1 font-mono text-[8px] text-muted uppercase"
            title="Large values are cut; '…' marks the omitted part."
          >
            Truncated preview
          </span>
        )}
      </div>
      <div className="mt-1.5 min-w-0 rounded bg-canvas p-2">
        {preview.kind === "json" ? (
          <ValueView value={preview.value} />
        ) : (
          <pre className="m-0 max-w-full overflow-x-auto font-mono text-[10px] whitespace-pre text-secondary">
            {preview.text}
          </pre>
        )}
      </div>
    </div>
  );
}

/**
 * What a standard step received and returned in this run: bounded input previews,
 * and the step's accepted typed output from the shared result route when the host
 * has one (the same for a flow step and a machine visit's step), else its preview.
 */
export function StepRunValues({
  node,
  result,
}: {
  node?: NodeSnapshotMsg;
  result?: StepResultRead;
}) {
  const stepIo = node?.stepIo;
  const output: StepValuePreview | undefined =
    result?.status === "ready"
      ? {
          kind: "json",
          typeName: stepIo?.output?.typeName ?? "",
          value: result.value,
          truncated: false,
        }
      : stepIo?.output;
  if (!stepIo && node && output) {
    return (
      <section aria-label="Output" className="min-w-0">
        <h3 className="inspector-section-title">Output</h3>
        <StepValue label="Return value" preview={output} />
      </section>
    );
  }
  if (!stepIo) {
    return (
      <p className="m-0 text-[11px] text-muted" role="status">
        {!node || node.status === "pending"
          ? "This step has not run yet."
          : "No inputs or output were captured for this step."}
      </p>
    );
  }
  return (
    <div className="grid min-w-0 gap-6">
      <section aria-label="Inputs" className="min-w-0">
        <h3 className="inspector-section-title">Inputs</h3>
        <div className="grid min-w-0 gap-3">
          {stepIo.inputs.length ? (
            stepIo.inputs.map((parameter) => (
              <StepValue
                key={parameter.name}
                label={parameter.name}
                preview={parameter.value}
              />
            ))
          ) : (
            <p className="m-0 text-[11px] text-muted">This step received no inputs.</p>
          )}
        </div>
      </section>
      <section aria-label="Output" className="min-w-0">
        <h3 className="inspector-section-title">Output</h3>
        {output ? (
          <StepValue label="Return value" preview={output} />
        ) : (
          <p className="m-0 text-[11px] text-muted" role="status">
            {node.status === "running"
              ? "Running…"
              : node.status === "failed"
                ? "No output: the step raised an error."
                : "No output was captured."}
          </p>
        )}
      </section>
    </div>
  );
}
