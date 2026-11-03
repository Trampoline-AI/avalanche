import { Check, Copy, X } from "lucide-react";
import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";

import { parseAgentLiveEvent } from "./agentTrace/common";
import type { OperatorApi } from "./api";
import { DESCRIPTOR_PAGE_SIZE } from "./detailProjection";
import type { PromptMessage, PromptSent, PromptText } from "./generated/agentSchemas";
import { DescriptorPageOrder, type AgentEventDescriptorMsg } from "./model";

interface EventScope {
  operatorInstanceId: string;
  asOfEventUlid: string;
  runId: string;
  nodeId: string;
  eventPageToken: string;
}

interface InitialPromptDialogProps extends EventScope {
  api: OperatorApi;
  liveEvents: AgentEventDescriptorMsg[];
}

type PromptState =
  | { status: "loading" }
  | { status: "ready"; prompt: PromptSent }
  | { status: "missing" }
  | { status: "error"; error: string };

const isPromptSent = (event: AgentEventDescriptorMsg) => event.agentEventKind === "prompt.sent";

// The step's prompt event, paging through its stored events from the first.
async function findPromptEvent(
  api: OperatorApi,
  scope: EventScope,
  signal: AbortSignal,
): Promise<string | undefined> {
  let pageToken = scope.eventPageToken;
  let cursor = "0";
  while (pageToken) {
    const page = await api.listAgentEventPage(
      {
        pageToken,
        afterEventSequence: cursor,
        beforeEventSequence: "0",
        pageSize: DESCRIPTOR_PAGE_SIZE,
        order: DescriptorPageOrder.FORWARD,
        expectedOperatorInstanceId: scope.operatorInstanceId,
        expectedAsOfEventUlid: scope.asOfEventUlid,
        expectedRunId: scope.runId,
        expectedNodeId: scope.nodeId,
      },
      signal,
    );
    const found = page.records.find(isPromptSent);
    if (found) return found.bodyToken;
    pageToken = page.nextPageToken;
    cursor = page.nextCursor;
  }
  return undefined;
}

async function loadPrompt(
  api: OperatorApi,
  scope: EventScope,
  liveBodyToken: string | undefined,
  signal: AbortSignal,
): Promise<PromptState> {
  const bodyToken = liveBodyToken ?? (await findPromptEvent(api, scope, signal));
  if (!bodyToken) return { status: "missing" };
  const { common } = parseAgentLiveEvent(await api.readJsonDetail(bodyToken, signal));
  if (common?.kind !== "prompt.sent") {
    throw new Error("The prompt event holds no prompt.");
  }
  return { status: "ready", prompt: common };
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => setCopied(true));
      }}
      className="icon-button grid size-[26px] shrink-0 cursor-pointer place-items-center rounded border border-line bg-panel p-0 text-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
    >
      {copied ? (
        <Check aria-hidden="true" className="size-3.5 text-success" strokeWidth={1.8} />
      ) : (
        <Copy aria-hidden="true" className="size-3.5" strokeWidth={1.8} />
      )}
    </button>
  );
}

const PRE_CLASS =
  "initial-prompt-text m-0 max-h-[50dvh] min-w-0 overflow-auto rounded border border-line bg-canvas p-3 font-mono text-[11px] leading-[1.55] whitespace-pre-wrap text-ink [overflow-wrap:anywhere]";

function json(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function OmittedNote({ chars }: { chars: number }) {
  if (chars <= 0) return null;
  return (
    <p className="m-0 text-[11px] text-muted" role="note">
      {chars.toLocaleString()} more characters were given to the model but are too large to keep
      in the trace.
    </p>
  );
}

function PromptTextView({ text }: { text: PromptText }) {
  return (
    <>
      <pre className={PRE_CLASS}>{text.text}</pre>
      <OmittedNote chars={text.omitted_chars} />
    </>
  );
}

function PromptSection({
  title,
  copyText,
  children,
}: {
  title: string;
  copyText: string;
  children: ReactNode;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="grid min-w-0 gap-2">
      <div className="flex items-center justify-between gap-3">
        <h3
          id={headingId}
          className="m-0 font-mono text-[10px] tracking-[.08em] text-secondary uppercase"
        >
          {title}
        </h3>
        <CopyButton text={copyText} label={`Copy ${title.toLowerCase()}`} />
      </div>
      {children}
    </section>
  );
}

function messageText(message: PromptMessage) {
  return message.parts
    .map((part) =>
      part.kind === "text"
        ? part.text.text
        : `[file: ${part.name ?? "unnamed"}${part.content_type ? `, ${part.content_type}` : ""}]`,
    )
    .join("\n\n");
}

function PromptBody({ prompt }: { prompt: PromptSent }) {
  return (
    <>
      <PromptSection
        title="System prompt"
        copyText={prompt.system.map((block) => block.text).join("\n\n")}
      >
        {prompt.system.length === 0 ? (
          <p className="m-0 text-[11px] text-muted">The model was given no system prompt.</p>
        ) : (
          prompt.system.map((block, index) => <PromptTextView key={index} text={block} />)
        )}
      </PromptSection>
      {prompt.messages.map((message, index) => (
        <PromptSection
          key={index}
          title={`${message.role} message${prompt.messages.length > 1 ? ` ${index + 1}` : ""}`}
          copyText={messageText(message)}
        >
          {message.parts.map((part, partIndex) =>
            part.kind === "text" ? (
              <PromptTextView key={partIndex} text={part.text} />
            ) : (
              <p
                key={partIndex}
                className="m-0 rounded border border-line px-3 py-2 font-mono text-[11px] text-secondary"
              >
                File {part.name ?? "without a name"}
                {part.content_type ? ` · ${part.content_type}` : ""}
                <span className="text-muted"> · its contents are not kept in the trace</span>
              </p>
            ),
          )}
        </PromptSection>
      ))}
      <PromptSection title={`Tools (${prompt.tools.length})`} copyText={json(prompt.tools)}>
        {prompt.tools.length === 0 ? (
          <p className="m-0 text-[11px] text-muted">The model was given no tools.</p>
        ) : (
          <div className="grid min-w-0 gap-1.5">
            {prompt.tools.map((tool, index) => (
              <details
                key={index}
                className="initial-prompt-tool min-w-0 rounded border border-line"
              >
                <summary className="cursor-pointer px-3 py-2 font-mono text-[11px] text-ink focus-visible:outline-2 focus-visible:outline-acid">
                  {tool.name}
                </summary>
                <div className="grid min-w-0 gap-2 border-t border-line p-3">
                  {tool.description && <PromptTextView text={tool.description} />}
                  <pre className={PRE_CLASS} aria-label={`${tool.name} input schema`}>
                    {json(tool.input_schema)}
                  </pre>
                </div>
              </details>
            ))}
          </div>
        )}
      </PromptSection>
      {prompt.output_schema !== null && (
        <PromptSection title="Output schema" copyText={json(prompt.output_schema)}>
          <pre className={PRE_CLASS}>{json(prompt.output_schema)}</pre>
        </PromptSection>
      )}
    </>
  );
}

/**
 * A quiet entry point to the prompt the agent started with. Nothing loads until the
 * dialog opens, and the prompt stays out of the trace and the step's values.
 */
export function InitialPromptDialog({
  api,
  liveEvents,
  operatorInstanceId,
  asOfEventUlid,
  runId,
  nodeId,
  eventPageToken,
}: InitialPromptDialogProps) {
  const liveBodyToken = useMemo(() => liveEvents.find(isPromptSent)?.bodyToken, [liveEvents]);
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<PromptState>({ status: "loading" });
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = dialogRef.current;
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    loadPrompt(
      api,
      { operatorInstanceId, asOfEventUlid, runId, nodeId, eventPageToken },
      liveBodyToken,
      controller.signal,
    )
      .then((next) => {
        if (!controller.signal.aborted) setState(next);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          error: error instanceof Error ? error.message : "Prompt unavailable",
        });
      });
    return () => controller.abort();
  }, [
    api,
    asOfEventUlid,
    eventPageToken,
    liveBodyToken,
    nodeId,
    open,
    operatorInstanceId,
    runId,
  ]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        className="initial-prompt-trigger cursor-pointer rounded border border-line bg-panel px-2 py-[5px] font-mono text-[9px] text-secondary uppercase hover:border-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
      >
        View initial prompt
      </button>
      <dialog
        ref={dialogRef}
        aria-labelledby={titleId}
        className="initial-prompt-dialog m-auto max-h-[85dvh] w-[min(56rem,calc(100vw-2rem))] overflow-hidden rounded border border-line bg-panel p-0 text-ink shadow-xl backdrop:bg-backdrop/40"
        onClose={() => {
          setOpen(false);
          triggerRef.current?.focus();
        }}
      >
        <div className="max-h-[calc(85dvh-2px)] overflow-auto overscroll-contain">
          <div className="grid min-w-0 gap-5 p-6">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 id={titleId} className="m-0 text-base">
                  Initial prompt
                </h2>
                <p className="m-0 mt-1 text-[11px] text-muted">
                  Everything the model was given before its first turn.
                </p>
              </div>
              <button
                type="button"
                aria-label="Close initial prompt"
                onClick={() => setOpen(false)}
                className="icon-button grid size-[30px] shrink-0 cursor-pointer place-items-center rounded border border-line bg-panel p-0 text-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
              >
                <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
              </button>
            </div>
            {!open ? null : state.status === "loading" ? (
              <p className="m-0 text-[11px] text-muted italic" role="status">
                Loading the initial prompt…
              </p>
            ) : state.status === "error" ? (
              <p className="m-0 text-[11px] text-danger [overflow-wrap:anywhere]" role="alert">
                {state.error}
              </p>
            ) : state.status === "missing" ? (
              <p className="m-0 text-[11px] text-muted">
                No initial prompt is recorded for this step. Its runtime may not report one, or
                the step has not reached its first model call.
              </p>
            ) : (
              <PromptBody prompt={state.prompt} />
            )}
          </div>
        </div>
      </dialog>
    </>
  );
}
