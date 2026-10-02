import type { MetricInput } from "./classifier";

const sourceLabels: Record<MetricInput["source"], string> = {
  trace: "Trace",
  output: "Output",
  input: "Input",
  custom: "Custom",
};

const sourceStyles: Record<MetricInput["source"], string> = {
  trace: "border-agent/20 bg-agent/10 text-agent",
  output: "border-classifier/20 bg-classifier-light text-classifier",
  input: "border-line bg-canvas text-secondary",
  custom: "border-line bg-panel text-muted",
};

export function MetricInputSummary({ inputs }: { inputs?: readonly MetricInput[] }) {
  if (!inputs?.length) return null;
  return (
    <ul
      aria-label="Metric sources"
      className="m-0 mt-1 flex list-none flex-wrap gap-x-3 gap-y-1 p-0 text-[10px]"
    >
      {inputs.map(({ source, selector }, index) => (
        <li
          key={`${source}:${selector}:${index}`}
          className="flex min-w-0 items-baseline gap-1.5"
        >
          <span className={`shrink-0 rounded border px-1.5 py-0.5 ${sourceStyles[source]}`}>
            {sourceLabels[source]}
          </span>
          {selector && <span className="font-mono [overflow-wrap:anywhere]">{selector}</span>}
        </li>
      ))}
    </ul>
  );
}
