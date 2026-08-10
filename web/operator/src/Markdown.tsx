import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";

export const MARKDOWN_SOURCE_CHUNK_CHARACTERS = 4_000;
export const NODE_MARKDOWN_EXCERPT_CHARACTERS = 480;

const MARKDOWN_ALLOWED_ELEMENTS = [
  "a",
  "blockquote",
  "br",
  "code",
  "em",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "li",
  "ol",
  "p",
  "pre",
  "strong",
  "ul",
];

interface MarkdownProps {
  children: string;
  className?: string;
  expandable?: boolean;
  sourceCharacterBudget?: number;
}

const MarkdownChunk = memo(({ source }: { source: string }) => (
  <ReactMarkdown allowedElements={MARKDOWN_ALLOWED_ELEMENTS} skipHtml>
    {source}
  </ReactMarkdown>
));
MarkdownChunk.displayName = "MarkdownChunk";

function MarkdownContent({
  children,
  className,
  expandable,
  sourceCharacterBudget,
}: Required<Pick<MarkdownProps, "children" | "expandable" | "sourceCharacterBudget">> &
  Pick<MarkdownProps, "className">) {
  const [visibleChunkCount, setVisibleChunkCount] = useState(1);
  const visibleChunks = [];
  for (
    let chunkIndex = 0;
    chunkIndex < visibleChunkCount && chunkIndex * sourceCharacterBudget < children.length;
    chunkIndex += 1
  ) {
    const start = chunkIndex * sourceCharacterBudget;
    visibleChunks.push(
      <MarkdownChunk
        key={chunkIndex}
        source={children.slice(start, start + sourceCharacterBudget)}
      />,
    );
  }
  const hasMore = visibleChunkCount * sourceCharacterBudget < children.length;

  return (
    <div className={className}>
      {visibleChunks}
      {expandable && hasMore && (
        <button
          type="button"
          className="descriptor-page-action inspector-markdown-more cursor-pointer rounded-md border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid"
          onClick={() => setVisibleChunkCount((current) => current + 1)}
        >
          Show more
        </button>
      )}
    </div>
  );
}

function MarkdownSource(
  props: Required<Pick<MarkdownProps, "children" | "expandable" | "sourceCharacterBudget">> &
    Pick<MarkdownProps, "className">,
) {
  return <MarkdownContent key={props.sourceCharacterBudget} {...props} />;
}

export function Markdown({
  children,
  className,
  expandable = true,
  sourceCharacterBudget = MARKDOWN_SOURCE_CHUNK_CHARACTERS,
}: MarkdownProps) {
  const boundedSourceCharacterBudget = Number.isFinite(sourceCharacterBudget)
    ? Math.min(MARKDOWN_SOURCE_CHUNK_CHARACTERS, Math.max(1, Math.floor(sourceCharacterBudget)))
    : MARKDOWN_SOURCE_CHUNK_CHARACTERS;
  return (
    <MarkdownSource
      key={children}
      className={className}
      expandable={expandable}
      sourceCharacterBudget={boundedSourceCharacterBudget}
    >
      {children}
    </MarkdownSource>
  );
}
