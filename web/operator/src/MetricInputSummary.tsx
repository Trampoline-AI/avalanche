import type { MetricInput } from "./classifier";

function inputPath({ source, selector }: MetricInput): string {
  if (source === "custom") return selector ? `custom: ${selector}` : source;
  const path = selector.replace(/^\[(['"])([A-Za-z_]\w*)\1\]/, "$2");
  if (!path) return source;
  return `${source}${path.startsWith("[") ? "" : "."}${path}`;
}

export function MetricInputSummary({ inputs }: { inputs?: readonly MetricInput[] }) {
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
            className="[overflow-wrap:anywhere]"
          >
            {inputPath(input)}
          </li>
        ))}
      </ul>
    </details>
  );
}
