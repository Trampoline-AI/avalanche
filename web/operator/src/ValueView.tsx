import { isUnknownRecord } from "./guards";

interface ValueViewProps {
  value: unknown;
  depth?: number;
}

export function ValueView({ value, depth = 0 }: ValueViewProps) {
  if (value === null) return <span className="value-null">null</span>;
  if (typeof value === "string") return <span className="value-string">{value}</span>;
  if (typeof value === "number" || typeof value === "boolean") {
    return <span className="value-scalar">{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    return (
      <ol className="value-list">
        {value.map((item, index) => (
          <li key={`${depth}-${index}`}>
            <ValueView value={item} depth={depth + 1} />
          </li>
        ))}
      </ol>
    );
  }
  if (isUnknownRecord(value)) {
    if (value.kind === "predict_rlm_file" && typeof value.path === "string") {
      return (
        <span className="file-value" title="Reported host path; contents are not copied">
          <span aria-hidden="true">↗</span>
          <span>
            <small>PredictRLM file</small>
            <code>{value.path}</code>
          </span>
        </span>
      );
    }
    if (value.kind === "unavailable" && typeof value.reason === "string") {
      return <span className="value-unavailable">Unavailable · {value.reason}</span>;
    }
    return (
      <dl className="value-object">
        {Object.entries(value).map(([key, item]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <ValueView value={item} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span className="value-unavailable">Unavailable</span>;
}
