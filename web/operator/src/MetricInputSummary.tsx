import type { MetricInput } from "./classifier";

function inputPath({ source, selector }: MetricInput): string {
  if (source === "custom") return selector ? `custom: ${selector}` : source;
  const path = selector.replace(/^\[(['"])([A-Za-z_]\w*)\1\]/, "$2");
  if (!path) return source;
  return `${source}${path.startsWith("[") ? "" : "."}${path}`;
}

export function MetricInputSummary({
  inputs,
  onOpenTrace,
}: {
  inputs?: readonly MetricInput[];
  onOpenTrace?: () => void;
}) {
  if (!inputs?.length) return null;
  return (
    <details className="mt-1 min-w-0 text-[11px]">
      <summary className="cursor-pointer text-secondary focus-visible:outline-2 focus-visible:outline-acid">
        Input
      </summary>
      <ul
        aria-label="Metric sources"
        className="m-0 mt-2 grid list-none gap-1 pl-3 font-mono text-secondary"
      >
        {inputs.map((input, index) => (
          <li
            key={`${input.source}:${input.selector}:${index}`}
            className={`[overflow-wrap:anywhere] ${input.source === "trace" ? "text-agent" : ""}`}
          >
            {input.source === "trace" && onOpenTrace ? (
              <button
                type="button"
                onClick={onOpenTrace}
                title="Open Trace tab"
                className="cursor-pointer border-0 bg-transparent p-0 text-left text-agent underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-agent"
              >
                {inputPath(input)}
              </button>
            ) : (
              inputPath(input)
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
