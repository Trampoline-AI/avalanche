import { formatCompositeSummary } from "./evaluations";
import { Percentage } from "./Percentage";

export function CompositeSummary({
  composites,
  className,
  compact = false,
}: {
  composites: Record<string, number>;
  className: string;
  compact?: boolean;
}) {
  const entries = Object.entries(composites);
  if (entries.length === 0) return null;
  const summary = formatCompositeSummary(composites);
  return (
    <span
      className={`${className} text-classifier`}
      title={summary}
      aria-label={`Evaluation composites: ${summary}`}
    >
      {entries.slice(0, 3).map(([name, value], index) => (
        <span key={name}>
          {index > 0 && " : "}
          {!compact && entries.length === 1 && `${name}: `}
          <Percentage value={value} decimal gradient />
        </span>
      ))}
    </span>
  );
}
