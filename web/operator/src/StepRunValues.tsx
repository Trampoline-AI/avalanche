import type { NodeSnapshotMsg } from "./model";
import type { StepValuePreview } from "./stepIo";
import { ValueView } from "./ValueView";

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

/** What a standard step received and returned in this run, as bounded previews. */
export function StepRunValues({ node }: { node?: NodeSnapshotMsg }) {
  const stepIo = node?.stepIo;
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
        {stepIo.output ? (
          <StepValue label="Return value" preview={stepIo.output} />
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
