import { formatCompositeSummary } from "./evaluations";
import { Percentage } from "./Percentage";

export function CompositeSummary({
  composites,
  className,
  compact = false,
  maxVisible,
}: {
  composites: Record<string, number>;
  className: string;
  compact?: boolean;
  maxVisible?: number;
}) {
  const entries = Object.entries(composites);
  if (entries.length === 0) return null;
  const summary = formatCompositeSummary(composites);
  const visibleEntries = maxVisible === undefined ? entries : entries.slice(0, maxVisible);
  const hiddenCount = entries.length - visibleEntries.length;
  return (
    <span
      className={`${className} text-classifier`}
      title={summary}
      aria-label={`Evaluation composites: ${summary}`}
    >
      <span className={maxVisible === undefined ? undefined : "min-w-0 truncate"}>
        {visibleEntries.map(([name, value], index) => (
          <span key={name}>
            {index > 0 && " · "}
            {!compact && entries.length === 1 && `${name}: `}
            <Percentage value={value} decimal gradient />
          </span>
        ))}
      </span>
      {hiddenCount > 0 && (
        <span
          className={`shrink-0 whitespace-nowrap text-black ${compact ? "text-[9px]" : "text-[8px]"}`}
        >
          and {hiddenCount} more
        </span>
      )}
    </span>
  );
}
