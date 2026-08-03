import { useId, useState } from "react";

import { isUnknownRecord } from "./guards";

interface ValueViewProps {
  value: unknown;
  depth?: number;
}

const CHILDREN_PER_GROUP = 100;
const STRING_PREVIEW_LENGTH = 240;
const MAX_DISCLOSURE_DEPTH = 12;

function plural(count: number, singular: string, pluralForm = `${singular}s`) {
  return count === 1 ? singular : pluralForm;
}

function countProperties(value: Record<string, unknown>) {
  let count = 0;
  for (const key in value) {
    if (Object.prototype.hasOwnProperty.call(value, key)) count += 1;
  }
  return count;
}

function LongString({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();

  return (
    <span>
      <span className="value-string" id={contentId}>
        {expanded ? value : `${value.slice(0, STRING_PREVIEW_LENGTH)}…`}
      </span>{" "}
      <button
        type="button"
        aria-controls={contentId}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        {expanded ? "Show less" : "Show full string"}
      </button>
    </span>
  );
}

function TruncatedCollection({
  kind,
  count,
}: {
  kind: "Array" | "Object";
  count: number;
}) {
  const itemLabel =
    kind === "Array"
      ? plural(count, "item")
      : plural(count, "property", "properties");
  const summary = `${kind} (${count} ${itemLabel}). Deeper values are not shown (maximum depth ${MAX_DISCLOSURE_DEPTH}).`;

  return (
    <span className="value-truncated" role="note" aria-label={summary}>
      {summary}
    </span>
  );
}

function ArrayValue({ value, depth }: { value: unknown[]; depth: number }) {
  const [expanded, setExpanded] = useState(false);
  const [visibleCount, setVisibleCount] = useState(CHILDREN_PER_GROUP);
  const contentId = useId();
  const renderedCount = Math.min(visibleCount, value.length);
  const groups = [];

  if (expanded) {
    for (let start = 0; start < renderedCount; start += CHILDREN_PER_GROUP) {
      const end = Math.min(start + CHILDREN_PER_GROUP, renderedCount);
      const children = [];
      for (let index = start; index < end; index += 1) {
        children.push(
          <li key={`${depth}-${index}`}>
            <ValueView value={value[index]} depth={depth + 1} />
          </li>,
        );
      }
      groups.push(
        <ol className="value-list" start={start + 1} key={start}>
          {children}
        </ol>,
      );
    }
  }

  const remaining = value.length - renderedCount;
  const nextCount = Math.min(CHILDREN_PER_GROUP, remaining);

  return (
    <div>
      <button
        type="button"
        aria-controls={contentId}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        Array ({value.length} {plural(value.length, "item")})
      </button>
      {expanded && (
        <div id={contentId}>
          {groups}
          {remaining > 0 && (
            <button
              type="button"
              aria-controls={contentId}
              onClick={() => setVisibleCount((current) => current + CHILDREN_PER_GROUP)}
            >
              Show {nextCount} more {plural(nextCount, "item")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ObjectValue({
  value,
  depth,
}: {
  value: Record<string, unknown>;
  depth: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const [visibleCount, setVisibleCount] = useState(CHILDREN_PER_GROUP);
  const contentId = useId();
  const propertyCount = countProperties(value);
  const renderedCount = Math.min(visibleCount, propertyCount);
  const groups: Array<Array<[string, unknown]>> = [];

  if (expanded && renderedCount > 0) {
    let seen = 0;
    for (const key in value) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      if (seen >= renderedCount) break;
      const groupIndex = Math.floor(seen / CHILDREN_PER_GROUP);
      const group = groups[groupIndex] ?? [];
      group.push([key, value[key]]);
      groups[groupIndex] = group;
      seen += 1;
    }
  }

  const remaining = propertyCount - renderedCount;
  const nextCount = Math.min(CHILDREN_PER_GROUP, remaining);

  return (
    <div>
      <button
        type="button"
        aria-controls={contentId}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        Object ({propertyCount} {plural(propertyCount, "property", "properties")})
      </button>
      {expanded && (
        <div id={contentId}>
          {groups.map((group, groupIndex) => (
            <dl className="value-object" key={groupIndex}>
              {group.map(([key, item]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>
                    <ValueView value={item} depth={depth + 1} />
                  </dd>
                </div>
              ))}
            </dl>
          ))}
          {remaining > 0 && (
            <button
              type="button"
              aria-controls={contentId}
              onClick={() => setVisibleCount((current) => current + CHILDREN_PER_GROUP)}
            >
              Show {nextCount} more {plural(nextCount, "property", "properties")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function ValueView({ value, depth = 0 }: ValueViewProps) {
  if (value === null) return <span className="value-null">null</span>;
  if (typeof value === "string") {
    return value.length > STRING_PREVIEW_LENGTH ? (
      <LongString value={value} />
    ) : (
      <span className="value-string">{value}</span>
    );
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return <span className="value-scalar">{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    return depth >= MAX_DISCLOSURE_DEPTH ? (
      <TruncatedCollection kind="Array" count={value.length} />
    ) : (
      <ArrayValue value={value} depth={depth} />
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
    if (depth >= MAX_DISCLOSURE_DEPTH) {
      return <TruncatedCollection kind="Object" count={countProperties(value)} />;
    }
    return <ObjectValue value={value} depth={depth} />;
  }
  return <span className="value-unavailable">Unavailable</span>;
}
