import { useId, useState } from "react";

import { isUnknownRecord } from "./guards";

interface ValueViewProps {
  value: unknown;
  depth?: number;
  onExpand?: (value: unknown, path: ReadonlyArray<string | number>) => void;
}

interface CollectionProps {
  value: unknown[] | Record<string, unknown>;
  depth: number;
  path: ReadonlyArray<string | number>;
  onExpand?: ValueViewProps["onExpand"];
}

const CHILDREN_PER_GROUP = 100;
const STRING_PREVIEW_LENGTH = 240;
const MAX_DISCLOSURE_DEPTH = 12;

function plural(count: number, singular: string, pluralForm = `${singular}s`) {
  return count === 1 ? singular : pluralForm;
}

function ownEntries(value: Record<string, unknown>): Array<[string, unknown]> {
  const entries: Array<[string, unknown]> = [];
  for (const key in value) {
    if (Object.prototype.hasOwnProperty.call(value, key)) entries.push([key, value[key]]);
  }
  return entries;
}

function collectionCount(value: unknown[] | Record<string, unknown>) {
  return Array.isArray(value) ? value.length : ownEntries(value).length;
}

function collectionSummary(value: unknown[] | Record<string, unknown>) {
  const count = collectionCount(value);
  return Array.isArray(value)
    ? `[${count} ${plural(count, "item")}]`
    : `{${count} ${plural(count, "property", "properties")}}`;
}

function LongString({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();

  return (
    <span className="value-long-string inline">
      <span
        className="value-string whitespace-pre-wrap text-mint [overflow-wrap:anywhere]"
        id={contentId}
      >
        {expanded ? value : `${value.slice(0, STRING_PREVIEW_LENGTH)}…`}
      </span>{" "}
      <button
        type="button"
        className="value-string-action cursor-pointer border-0 bg-transparent p-0 text-left font-mono text-[9px]/[1.45] text-acid"
        aria-controls={contentId}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        {expanded ? "Show less" : "Show full string"}
      </button>
    </span>
  );
}

function TruncatedCollection({ value }: { value: unknown[] | Record<string, unknown> }) {
  const count = collectionCount(value);
  const itemLabel = Array.isArray(value)
    ? plural(count, "item")
    : plural(count, "property", "properties");
  const summary = `${count} ${itemLabel}. Deeper values are not shown (maximum depth ${MAX_DISCLOSURE_DEPTH}).`;

  return (
    <span className="value-truncated text-muted" role="note" aria-label={summary}>
      {collectionSummary(value)} · maximum depth reached
    </span>
  );
}

function ScalarValue({ value }: { value: unknown }) {
  if (value === null) return <span className="value-null text-muted">null</span>;
  if (typeof value === "string") {
    return value.length > STRING_PREVIEW_LENGTH ? (
      <LongString value={value} />
    ) : (
      <span className="value-string whitespace-pre-wrap text-mint [overflow-wrap:anywhere]">
        {value}
      </span>
    );
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return <span className="value-scalar text-acid">{String(value)}</span>;
  }
  if (isUnknownRecord(value)) {
    if (value.kind === "predict_rlm_file" && typeof value.path === "string") {
      return (
        <span
          className="file-value flex min-w-48 gap-[9px] rounded-[7px] border border-acid p-[9px] text-acid [&_small]:block [&_small]:text-[7px] [&_small]:text-muted [&_small]:uppercase [&_code]:mt-[3px] [&_code]:block [&_code]:whitespace-normal [&_code]:text-[9px] [&_code]:[overflow-wrap:anywhere]"
          title="Reported host path; contents are not copied"
        >
          <span aria-hidden="true">↗</span>
          <span>
            <small>PredictRLM file</small>
            <code>{value.path}</code>
          </span>
        </span>
      );
    }
    if (value.kind === "unavailable" && typeof value.reason === "string") {
      return (
        <span className="value-unavailable text-[9px] text-amber">
          Unavailable · {value.reason}
        </span>
      );
    }
  }
  return <span className="value-unavailable text-[9px] text-amber">Unavailable</span>;
}

function isCollection(value: unknown): value is unknown[] | Record<string, unknown> {
  return (
    Array.isArray(value) ||
    (isUnknownRecord(value) &&
      !(
        (value.kind === "predict_rlm_file" && typeof value.path === "string") ||
        (value.kind === "unavailable" && typeof value.reason === "string")
      ))
  );
}

function CollectionNode({
  label,
  value,
  depth,
  path,
  onExpand,
}: CollectionProps & { label: string }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const atDepthLimit = depth >= MAX_DISCLOSURE_DEPTH;

  if (atDepthLimit) return <TruncatedCollection value={value} />;

  return (
    <div className="value-collection min-w-0">
      <button
        type="button"
        className="value-disclosure inline-flex cursor-pointer items-baseline gap-[5px] border-0 bg-transparent p-0 text-left font-mono text-[9px]/[1.45] text-acid"
        aria-controls={contentId}
        aria-expanded={expanded}
        aria-label={`${expanded ? "Collapse" : "Expand"} ${label}`}
        onClick={() => {
          if (!expanded) onExpand?.(value, path);
          setExpanded((current) => !current);
        }}
      >
        <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        <span>{collectionSummary(value)}</span>
      </button>
      {expanded && (
        <div
          className="value-child-group mt-[5px] min-w-80 border-l border-line pl-3"
          id={contentId}
          role="group"
        >
          <CollectionChildren value={value} depth={depth} path={path} onExpand={onExpand} />
        </div>
      )}
    </div>
  );
}

function CollectionChildren({ value, depth, path, onExpand }: CollectionProps) {
  const [visibleCount, setVisibleCount] = useState(CHILDREN_PER_GROUP);
  const entries: Array<[string | number, unknown]> = Array.isArray(value)
    ? value.slice(0, visibleCount).map((item, index) => [index, item])
    : ownEntries(value).slice(0, visibleCount);
  const count = collectionCount(value);
  const remaining = count - entries.length;
  const nextCount = Math.min(CHILDREN_PER_GROUP, remaining);

  if (!count) {
    return (
      <span className="value-empty-collection text-muted">
        {Array.isArray(value) ? "[]" : "{}"}
      </span>
    );
  }

  return (
    <>
      <ul className="value-group m-0 min-w-80 list-none p-0" role="group">
        {entries.map(([key, item]) => {
          const childPath = [...path, key];
          const nested = isCollection(item);
          return (
            <li
              className="value-tree-item min-w-0 border-t border-line first:border-t-0"
              role="treeitem"
              key={`${typeof key}:${String(key)}`}
            >
              <div className="value-row grid min-w-80 grid-cols-[fit-content(12rem)_.55rem_minmax(12rem,1fr)] items-start gap-x-0.5 py-[7px]">
                <span className="value-key min-w-0 font-mono text-[9px]/[1.45] text-muted [overflow-wrap:anywhere]">
                  {Array.isArray(value) ? `[${key}]` : key}
                </span>
                <span className="value-separator text-center text-muted" aria-hidden="true">
                  :
                </span>
                <div className="value-content min-w-48 [overflow-wrap:anywhere]">
                  {nested ? (
                    <CollectionNode
                      label={String(key)}
                      value={item}
                      depth={depth + 1}
                      path={childPath}
                      onExpand={onExpand}
                    />
                  ) : (
                    <ScalarValue value={item} />
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {remaining > 0 && (
        <button
          type="button"
          className="value-more my-2 cursor-pointer border-0 bg-transparent p-0 text-left font-mono text-[9px]/[1.45] text-acid"
          onClick={() => setVisibleCount((current) => current + CHILDREN_PER_GROUP)}
        >
          Show {nextCount} more{" "}
          {Array.isArray(value)
            ? plural(nextCount, "item")
            : plural(nextCount, "property", "properties")}
        </button>
      )}
    </>
  );
}

export function ValueView({ value, depth = 0, onExpand }: ValueViewProps) {
  if (!isCollection(value)) return <ScalarValue value={value} />;
  if (depth >= MAX_DISCLOSURE_DEPTH) return <TruncatedCollection value={value} />;

  return (
    <div
      className="value-tree min-w-0 max-w-full overflow-x-auto text-[10px]"
      role="tree"
      aria-label="JSON value"
    >
      <CollectionChildren value={value} depth={depth} path={[]} onExpand={onExpand} />
    </div>
  );
}
