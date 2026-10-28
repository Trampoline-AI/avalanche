import { type ReactNode, type RefObject, useEffect, useMemo, useRef, useState } from "react";

import type {
  AgentTracePredictCall,
  AgentTraceStep,
  AgentTraceStepSummary,
  AgentTraceToolCall,
  TraceTokenUsage,
  TraceTurnTokens,
} from "./agentTrace/types";
import { activityLabel } from "./agentTrace/viewers";
import { compareSequence } from "./detailProjection";
import type { AgentEventDescriptorMsg } from "./model";
import { PythonSource } from "./PythonSource";
import { ValueView } from "./ValueView";

export type AgentTraceTurnDetail =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; step: AgentTraceStep }
  | { status: "error"; error: string; retryable: boolean };

export type AgentTraceTurnSummary =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; step: AgentTraceStepSummary }
  | { status: "error"; error: string };

export interface AgentTraceTurn {
  descriptor: AgentEventDescriptorMsg;
  summary: AgentTraceTurnSummary;
  detail: AgentTraceTurnDetail;
}

interface AgentTraceExplorerProps {
  scopeKey: string;
  turns: AgentTraceTurn[];
  lifecycleEvents: AgentEventDescriptorMsg[];
  running: boolean;
  loading: boolean;
  error?: string;
  hasMore: boolean;
  scrollRef: RefObject<HTMLDivElement | null>;
  onLoadTurn: (event: AgentEventDescriptorMsg) => void;
  onLoadMore: () => void;
  onTurnDetailOpenChange: (event: AgentEventDescriptorMsg, open: boolean) => void;
  onScroll: (element: HTMLDivElement) => void;
}

const NUMBER_FORMAT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function formatDuration(durationMs: number | string | undefined) {
  if (durationMs === undefined) return "—";
  const parsed = typeof durationMs === "string" ? Number(durationMs) : durationMs;
  if (!Number.isFinite(parsed)) return "—";
  if (parsed < 1000) return `${NUMBER_FORMAT.format(parsed)}ms`;
  return `${NUMBER_FORMAT.format(parsed / 1000)}s`;
}

function finishLabel(finishReason: string | undefined) {
  if (finishReason === "stop") return "done";
  return finishReason;
}

function usageLabel(usage: TraceTokenUsage) {
  const tokens = `${NUMBER_FORMAT.format(usage.inputTokens)} in / ${NUMBER_FORMAT.format(usage.outputTokens)} out`;
  const cost = usage.cost > 0 ? ` · $${usage.cost.toFixed(4)}` : "";
  const cache =
    usage.cacheHits > 0
      ? ` · ${usage.cacheHits} cache hit${usage.cacheHits === 1 ? "" : "s"}`
      : "";
  return `${tokens}${cost}${cache}`;
}

function turnUsageLabel(usage: TraceTurnTokens) {
  const cost = usage.cost === undefined ? "" : ` · $${usage.cost.toFixed(4)}`;
  return `${NUMBER_FORMAT.format(usage.inputTokens)} in / ${NUMBER_FORMAT.format(usage.outputTokens)} out${cost}`;
}

function TraceSection({
  label,
  children,
  onOpenChange,
}: {
  label: string;
  children: ReactNode;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <details
      className="trace-section min-w-0 border-t border-line py-2.5 first:border-t-0"
      open={open}
      onToggle={(event) => {
        const nextOpen = event.currentTarget.open;
        setOpen(nextOpen);
        onOpenChange?.(nextOpen);
      }}
    >
      <summary className="cursor-pointer font-mono text-[9px] font-semibold text-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid">
        {label}
      </summary>
      {open && <div className="mt-2 min-w-0">{children}</div>}
    </details>
  );
}

function TraceErrorOutput({ error }: { error: string }) {
  return (
    <pre className="m-0 max-h-56 overflow-auto whitespace-pre-wrap font-mono text-[9px]/[1.55] text-danger [overflow-wrap:anywhere]">
      {error}
    </pre>
  );
}

function ToolCalls({ calls }: { calls: AgentTraceToolCall[] }) {
  if (!calls.length) return <p className="m-0 text-[9px] text-muted">No tool calls.</p>;
  return (
    <div className="grid gap-1.5">
      {calls.map((call, index) => (
        <details
          className={`rounded border px-2.5 py-2 ${call.error ? "border-danger bg-error-soft" : "border-line bg-canvas"}`}
          key={`${index}:${call.name}`}
        >
          <summary className="cursor-pointer text-[10px] font-semibold text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid">
            {call.error ? "Failed tool" : "Tool"} · {call.name}
            <span className="ml-1.5 font-mono text-[8px] font-normal text-muted">
              {formatDuration(call.durationMs)}
            </span>
          </summary>
          <div className="mt-2 grid gap-2">
            <div>
              <h5 className="m-0 mb-1 font-mono text-[8px] text-muted uppercase">Arguments</h5>
              <ValueView value={call.arguments} />
            </div>
            <div>
              <h5
                className={`m-0 mb-1 font-mono text-[8px] uppercase ${call.error ? "text-danger" : "text-muted"}`}
              >
                {call.error ? "Error" : "Result"}
              </h5>
              {call.error ? (
                <TraceErrorOutput error={call.error} />
              ) : (
                <ValueView value={call.result} />
              )}
            </div>
          </div>
        </details>
      ))}
    </div>
  );
}

function PredictCalls({ calls }: { calls: AgentTracePredictCall[] }) {
  if (!calls.length) return <p className="m-0 text-[9px] text-muted">No predict calls.</p>;
  return (
    <div className="grid gap-1.5">
      {calls.map((call, index) => (
        <details
          className={`rounded border px-2.5 py-2 ${call.error ? "border-danger bg-error-soft" : "border-line bg-canvas"}`}
          key={`${index}:${call.signature}:${call.model}`}
        >
          <summary className="cursor-pointer text-[10px] font-semibold text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid">
            {call.error ? "Failed predict" : "Predict"} · {call.signature}
            <span className="ml-1.5 font-mono text-[8px] font-normal text-muted">
              {call.model} · {formatDuration(call.durationMs)}
            </span>
          </summary>
          <div className="mt-2 grid gap-2">
            {call.instructions && (
              <p className="m-0 whitespace-pre-wrap text-[9px] leading-relaxed text-secondary">
                {call.instructions}
              </p>
            )}
            <div>
              <h5 className="m-0 mb-1 font-mono text-[8px] text-muted uppercase">Input</h5>
              <ValueView value={call.input} />
            </div>
            <div>
              <h5
                className={`m-0 mb-1 font-mono text-[8px] uppercase ${call.error ? "text-danger" : "text-muted"}`}
              >
                {call.error ? "Error" : "Output"}
              </h5>
              {call.error ? (
                <TraceErrorOutput error={call.error} />
              ) : (
                <ValueView value={call.output} />
              )}
            </div>
            <p className="m-0 font-mono text-[8px] text-muted">
              {usageLabel(call.usage)}
              {call.lm?.finishReason ? ` · ${call.lm.finishReason}` : ""}
            </p>
          </div>
        </details>
      ))}
    </div>
  );
}

function DeferredTurnDetail({
  detail,
  onLoadTurn,
  children,
}: {
  detail: AgentTraceTurnDetail;
  onLoadTurn: () => void;
  children: (step: AgentTraceStep) => ReactNode;
}) {
  if (detail.status === "ready") return children(detail.step);
  if (detail.status === "loading") {
    return (
      <p className="m-0 text-[9px] text-muted italic" role="status">
        Loading step detail…
      </p>
    );
  }
  return (
    <div className="grid gap-2">
      {detail.status === "error" && (
        <p className="m-0 text-[9px] text-danger" role="alert">
          Step detail unavailable: {detail.error}
        </p>
      )}
      {(detail.status === "idle" || detail.retryable) && (
        <button
          type="button"
          className="w-fit cursor-pointer rounded border border-line bg-panel px-2 py-1 font-mono text-[9px] text-acid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
          onClick={onLoadTurn}
        >
          {detail.status === "error" ? "Retry step detail" : "Reload step detail"}
        </button>
      )}
    </div>
  );
}

function TurnSecondaryDetails({
  descriptor,
  summary,
  detail,
  showFullOutput,
  onShowFullOutput,
  onLoadTurn,
  onTurnDetailOpenChange,
}: {
  descriptor: AgentEventDescriptorMsg;
  summary: AgentTraceStepSummary;
  detail: AgentTraceTurnDetail;
  showFullOutput: boolean;
  onShowFullOutput: () => void;
  onLoadTurn: () => void;
  onTurnDetailOpenChange: (event: AgentEventDescriptorMsg, open: boolean) => void;
}) {
  const openSections = useRef(new Set<string>());

  useEffect(() => {
    onTurnDetailOpenChange(descriptor, openSections.current.size > 0);
    return () => onTurnDetailOpenChange(descriptor, false);
  }, [descriptor, onTurnDetailOpenChange]);

  function setSectionOpen(section: string, open: boolean) {
    if (open) openSections.current.add(section);
    else openSections.current.delete(section);
    onTurnDetailOpenChange(descriptor, openSections.current.size > 0);
    if (open && detail.status === "idle") onLoadTurn();
  }

  return (
    <div className="trace-turn-detail border-t border-line px-3 pb-1">
      {summary.hasExecution && (
        <>
          <TraceSection
            label="Generated Python"
            onOpenChange={(open) => setSectionOpen("code", open)}
          >
            <DeferredTurnDetail detail={detail} onLoadTurn={onLoadTurn}>
              {(step) => (
                <div className="h-56 max-h-[45vh] min-h-32 overflow-auto rounded border border-line bg-canvas">
                  <PythonSource source={step.execution?.code ?? ""} />
                </div>
              )}
            </DeferredTurnDetail>
          </TraceSection>
          <TraceSection
            label={summary.error ? "Sandbox error output" : "Sandbox output"}
            onOpenChange={(open) => setSectionOpen("output", open)}
          >
            <DeferredTurnDetail detail={detail} onLoadTurn={onLoadTurn}>
              {(step) => {
                const execution = step.execution;
                if (execution === undefined) return null;
                const outputDiffers = execution.untruncatedOutput !== execution.output;
                return (
                  <>
                    <pre
                      className={`m-0 max-h-56 overflow-auto whitespace-pre-wrap rounded border border-terminal-line bg-terminal-surface px-3 py-2.5 font-mono text-[8px] leading-[1.55] [overflow-wrap:anywhere] ${step.error ? "text-terminal-error" : "text-terminal-ink"}`}
                    >
                      {showFullOutput ? execution.untruncatedOutput : execution.output}
                    </pre>
                    {outputDiffers && (
                      <button
                        type="button"
                        className="mt-2 cursor-pointer border-0 bg-transparent p-0 font-mono text-[8px] text-acid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
                        aria-expanded={showFullOutput}
                        onClick={onShowFullOutput}
                      >
                        {showFullOutput ? "Show shortened output" : "Show full output"}
                      </button>
                    )}
                  </>
                );
              }}
            </DeferredTurnDetail>
          </TraceSection>
        </>
      )}
      <TraceSection
        label={`Tools (${summary.toolCallCount})`}
        onOpenChange={(open) => setSectionOpen("tools", open)}
      >
        <DeferredTurnDetail detail={detail} onLoadTurn={onLoadTurn}>
          {(step) => <ToolCalls calls={step.toolCalls} />}
        </DeferredTurnDetail>
      </TraceSection>
      {summary.usage.sub !== undefined && (
        <TraceSection
          label={`Predict calls (${summary.predictCallCount})`}
          onOpenChange={(open) => setSectionOpen("predict", open)}
        >
          <DeferredTurnDetail detail={detail} onLoadTurn={onLoadTurn}>
            {(step) => <PredictCalls calls={step.predictCalls} />}
          </DeferredTurnDetail>
        </TraceSection>
      )}
      {/* Provider reasoning is long and rarely read: last, collapsed, and only when kept. */}
      {summary.hasReasoning && (
        <TraceSection
          label="Model reasoning"
          onOpenChange={(open) => setSectionOpen("reasoning", open)}
        >
          <DeferredTurnDetail detail={detail} onLoadTurn={onLoadTurn}>
            {(step) => (
              <p className="m-0 max-h-56 overflow-auto whitespace-pre-wrap text-[10px] leading-[1.6] text-muted [overflow-wrap:anywhere]">
                {step.reasoning}
              </p>
            )}
          </DeferredTurnDetail>
        </TraceSection>
      )}
    </div>
  );
}

export function AgentTraceExplorer({
  scopeKey,
  turns,
  lifecycleEvents,
  running,
  loading,
  error,
  hasMore,
  scrollRef,
  onLoadTurn,
  onTurnDetailOpenChange,
  onLoadMore,
  onScroll,
}: AgentTraceExplorerProps) {
  const [fullOutputTurns, setFullOutputTurns] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    setFullOutputTurns(new Set());
  }, [scopeKey]);

  useEffect(() => {
    for (const turn of turns) {
      if (turn.summary.status === "idle") onLoadTurn(turn.descriptor);
    }
  }, [onLoadTurn, turns]);

  const orderedTurns = useMemo(
    () =>
      [...turns].sort((left, right) => {
        const leftIteration =
          left.summary.status === "ready" ? left.summary.step.iteration : left.descriptor.turn;
        const rightIteration =
          right.summary.status === "ready"
            ? right.summary.step.iteration
            : right.descriptor.turn;
        if (
          leftIteration !== undefined &&
          rightIteration !== undefined &&
          leftIteration !== rightIteration
        ) {
          return leftIteration - rightIteration;
        }
        return compareSequence(left.descriptor.eventSequence, right.descriptor.eventSequence);
      }),
    [turns],
  );

  const currentActivity = lifecycleEvents.at(-1);

  return (
    <section
      aria-label="Agent trace"
      className="inspector-panel inspector-trace-panel h-full min-h-0 min-w-0"
    >
      <div
        className="inspector-trace-explorer h-full min-h-0 min-w-0 overflow-auto overscroll-contain"
        ref={scrollRef}
        onScroll={(event) => onScroll(event.currentTarget)}
      >
        <div className="flex min-h-full min-w-0 flex-col gap-3 px-5 pt-[18px] pb-[30px]">
          {running && currentActivity && (
            <div
              className="trace-current-activity flex items-center gap-2 rounded border border-agent-line bg-agent-surface px-2.5 py-2 text-[9px] text-secondary"
              role="status"
            >
              <span className="relative flex size-2 flex-none" aria-hidden="true">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-agent opacity-50 motion-reduce:animate-none" />
                <span className="relative inline-flex size-2 rounded-full bg-agent" />
              </span>
              <span>
                {activityLabel(currentActivity.nativeEventKind, currentActivity.agentEventKind)}
                {currentActivity.turn ? ` · turn ${currentActivity.turn}` : ""}
              </span>
            </div>
          )}

          {error && (
            <p
              className="inspector-error rounded border border-danger p-2.5 text-[10px] text-danger [overflow-wrap:anywhere]"
              role="alert"
            >
              {error}
            </p>
          )}
          {loading && !orderedTurns.length && (
            <p className="inspector-loading text-[11px] text-muted italic" role="status">
              Loading retained trace…
            </p>
          )}

          {(!error || orderedTurns.length > 0) && (
            <div>
              <div className="turn-list grid min-w-0 gap-2.5">
                {orderedTurns.map(({ descriptor, summary, detail }, index) => {
                  const turnKey = descriptor.bodyToken || descriptor.eventSequence;
                  const step = summary.status === "ready" ? summary.step : undefined;
                  const iteration = step?.iteration ?? descriptor.turn ?? index + 1;
                  const failed = descriptor.error || step?.error;
                  const finishReason = failed ? undefined : finishLabel(step?.lm?.finishReason);
                  // Only runtimes with a sub-model make predict calls.
                  const hasPredictCalls = step?.usage.sub !== undefined;
                  // Before the turn loads, the count covers predict calls too.
                  const toolCallCount = step?.toolCallCount ?? descriptor.toolCallCount;
                  return (
                    <article
                      className={`turn-row overflow-hidden rounded border bg-panel ${failed ? "border-danger" : "border-line"}`}
                      key={turnKey}
                    >
                      <header className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2.5 bg-canvas px-3 py-2.5">
                        <span
                          className={`mt-0.5 grid size-5 place-items-center rounded-full border font-mono text-[8px] ${failed ? "border-danger text-danger" : "border-acid text-acid"}`}
                          aria-hidden="true"
                        >
                          {failed ? "×" : iteration}
                        </span>
                        <div className="min-w-0">
                          <strong className="block text-[10px] text-ink">
                            Turn {iteration}
                          </strong>
                          <p className="mt-0.5 mb-0 whitespace-normal font-mono text-[8px] leading-[1.6] text-muted [overflow-wrap:anywhere]">
                            {formatDuration(step?.durationMs ?? descriptor.durationMs)}
                            {` · ${toolCallCount} tool${toolCallCount === 1 ? "" : "s"}`}
                            {hasPredictCalls ? ` · ${step.predictCallCount} predict` : ""}
                            {step?.usage.main
                              ? ` · main ${turnUsageLabel(step.usage.main)}`
                              : ""}
                            {step?.usage.sub ? ` · sub ${turnUsageLabel(step.usage.sub)}` : ""}
                            {finishReason ? ` · ${finishReason}` : ""}
                            {failed ? " · error" : ""}
                          </p>
                        </div>
                      </header>

                      <section className="border-t border-line px-3 py-3">
                        {step ? (
                          step.message ? (
                            <p className="m-0 whitespace-pre-wrap text-[13px] leading-[1.7] text-ink [overflow-wrap:anywhere]">
                              {step.message}
                            </p>
                          ) : step.submitsAnswer ? (
                            <p className="m-0 text-[10px] text-muted italic">
                              Submitted the answer; its outputs are under Tools.
                            </p>
                          ) : (
                            <p className="m-0 text-[10px] text-muted italic">
                              The model wrote no text this turn.
                            </p>
                          )
                        ) : summary.status === "error" ? (
                          <div>
                            <p className="m-0 text-[10px] text-danger" role="alert">
                              Turn unavailable: {summary.error}
                            </p>
                            <button
                              type="button"
                              className="mt-2 cursor-pointer rounded border border-line bg-panel px-2 py-1 font-mono text-[9px] text-acid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
                              onClick={() => onLoadTurn(descriptor)}
                              aria-label={`Retry turn ${iteration}`}
                            >
                              Retry
                            </button>
                          </div>
                        ) : (
                          <p className="m-0 text-[10px] text-muted italic" role="status">
                            Loading turn…
                          </p>
                        )}
                      </section>

                      {step && (
                        <TurnSecondaryDetails
                          descriptor={descriptor}
                          summary={step}
                          detail={detail}
                          showFullOutput={fullOutputTurns.has(turnKey)}
                          onLoadTurn={() => onLoadTurn(descriptor)}
                          onTurnDetailOpenChange={onTurnDetailOpenChange}
                          onShowFullOutput={() =>
                            setFullOutputTurns((current) => {
                              const next = new Set(current);
                              if (next.has(turnKey)) next.delete(turnKey);
                              else next.add(turnKey);
                              return next;
                            })
                          }
                        />
                      )}
                    </article>
                  );
                })}
              </div>
              {!loading && !orderedTurns.length && !error && (
                <p className="empty-copy text-[11px] text-muted">
                  No executable turn has been retained.
                </p>
              )}
              {loading && orderedTurns.length > 0 && (
                <p
                  className="inspector-loading mt-2 text-[11px] text-muted italic"
                  role="status"
                >
                  Loading more retained trace…
                </p>
              )}
              {!loading && !hasMore && orderedTurns.length > 0 && (
                <p className="inspector-end-state mt-3 text-center font-mono text-[8px] text-muted uppercase">
                  End of retained trace
                </p>
              )}
            </div>
          )}

          {hasMore && (
            <button
              type="button"
              className="descriptor-page-action cursor-pointer rounded border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
              disabled={loading}
              aria-busy={loading}
              onClick={onLoadMore}
            >
              {loading ? "Loading events…" : "Load more trace"}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
