const wholePercent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 0,
});
const decimalPercent = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

export function percentageColor(value: number): string {
  if (value <= 0.5) {
    return `color-mix(in srgb, var(--color-danger), var(--color-score-mid) ${value * 200}%)`;
  }
  return `color-mix(in srgb, var(--color-score-mid), var(--color-success) ${(value - 0.5) * 200}%)`;
}

export function Percentage({
  value,
  decimal = false,
  gradient = false,
}: {
  value: number;
  decimal?: boolean;
  gradient?: boolean;
}) {
  return (
    <span
      className="shrink-0 tabular-nums"
      style={gradient ? { color: percentageColor(value) } : undefined}
    >
      {(decimal ? decimalPercent : wholePercent).format(value)}
    </span>
  );
}
