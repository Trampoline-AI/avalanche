import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, ListFilter, X } from "lucide-react";

import type { OperatorApi } from "./api";
import { ClassifierInvocationDetails, ClassifierQuestions } from "./ClassifierDetails";
import {
  type ClassifierDeclaration,
  type ClassifierEntry,
  type ClassifierJson,
  type ClassifierInvocation,
  parseClassifierInvocation,
} from "./classifier";
import {
  boundDescriptors,
  compareSequence,
  DESCRIPTOR_PAGE_SIZE,
  DESCRIPTOR_WINDOW_SIZE,
  DETAIL_CACHE_MAX_BYTES,
  type DescriptorPageState,
  measuredByteCost,
} from "./detailProjection";
import {
  type ClassifierEventDescriptorMsg,
  DescriptorPageOrder,
  type FlowInfoMsg,
  type NodeSnapshotMsg,
  type RunSnapshotMsg,
} from "./model";
import { PythonSource } from "./PythonSource";

const EMPTY_EVENTS: ClassifierEventDescriptorMsg[] = [];
const EMPTY_PAGE: DescriptorPageState<ClassifierEventDescriptorMsg> = {
  records: EMPTY_EVENTS,
  nextPageToken: "",
  nextCursor: "0",
};
const DETAIL_CACHE_MAX_ENTRIES = 8;
const BUTTON_CLASS =
  "cursor-pointer rounded border border-line bg-panel px-2 py-1 text-[11px] text-secondary hover:text-ink disabled:cursor-default disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-acid";

interface ClassifierInspectorProps {
  api: OperatorApi;
  nodeId: string;
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  declaration?: ClassifierDeclaration;
  declarationError?: string;
  liveEvents?: ClassifierEventDescriptorMsg[];
  embedded?: boolean;
  definitionLabel?: string;
  onClose: () => void;
}

// Lifecycle snapshots describe one call. A delayed running snapshot must never
// replace terminal evidence, including when older pages overlap live updates.
function groupInvocations(records: Iterable<ClassifierEventDescriptorMsg>) {
  const grouped = new Map<string, ClassifierEventDescriptorMsg>();
  for (const event of records) {
    const previous = grouped.get(event.invocationId);
    if (
      !previous ||
      (previous.eventKind === "running" && event.eventKind !== "running") ||
      ((previous.eventKind === "running") === (event.eventKind === "running") &&
        compareSequence(event.eventSequence, previous.eventSequence) > 0)
    )
      grouped.set(event.invocationId, event);
  }
  return grouped;
}

function summarizeInput(input: ClassifierEntry): string {
  if (input === null) return "Input not captured";
  if (typeof input === "string")
    return input === "" ? '""' : input.slice(0, 160).replace(/\s+/g, " ");
  const entries: [string, ClassifierJson][] = [];
  if (Array.isArray(input)) {
    for (let index = 0; index < Math.min(input.length, 3); index++)
      entries.push([String(index), input[index]]);
  } else {
    for (const key in input) {
      entries.push([key, input[key]]);
      if (entries.length === 3) break;
    }
  }
  return `${Array.isArray(input) ? `Array (${input.length})` : "Object"}${entries.length ? " · " : ""}${entries
    .map(([key, value]) => {
      const preview =
        typeof value === "string"
          ? value.slice(0, 48).replace(/\s+/g, " ")
          : value !== null && typeof value === "object"
            ? Array.isArray(value)
              ? "[…]"
              : "{…}"
            : String(value);
      return `${key.slice(0, 32)}: ${preview}`;
    })
    .join(" · ")}`;
}

interface CachedInvocation {
  invocation: ClassifierInvocation;
  byteCost: number;
  inputPreview: string;
}
type DetailStatus =
  | { status: "loading" | "ready" | "released" }
  | { status: "error"; error: string; retryable: boolean };
interface HistoryState {
  api: OperatorApi;
  page: DescriptorPageState<ClassifierEventDescriptorMsg>;
  loaded: boolean;
  loading: boolean;
  newerHistoryEvicted: boolean;
  error?: string;
  cache: Map<string, CachedInvocation>;
  details: Map<string, DetailStatus>;
}
function emptyHistory(api: OperatorApi): HistoryState {
  return {
    api,
    page: EMPTY_PAGE,
    loaded: false,
    loading: false,
    newerHistoryEvicted: false,
    cache: new Map(),
    details: new Map(),
  };
}

function ClassifierHistory({
  api,
  run,
  node,
  nodeId,
  liveEvents,
}: {
  api: OperatorApi;
  run: RunSnapshotMsg;
  node?: NodeSnapshotMsg;
  nodeId: string;
  liveEvents: ClassifierEventDescriptorMsg[];
}) {
  const [selection, setSelection] = useState<{
    api: OperatorApi;
    invocationId: string | null;
  }>();
  const [stored, setStored] = useState(() => emptyHistory(api));
  const state = stored.api === api ? stored : emptyHistory(api);
  const pageController = useRef<AbortController | undefined>(undefined);
  const detailControllers = useRef(new Map<string, AbortController>());
  const runId = run.summary?.runId ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const operatorInstanceId = run.operatorInstanceId;
  const asOfEventUlid = run.asOfEventUlid;

  const loadPage = useCallback(
    (pageToken: string, beforeEventSequence = "0") => {
      if (pageController.current) return;
      const controller = new AbortController();
      pageController.current = controller;
      setStored((current) => ({
        ...(current.api === api ? current : emptyHistory(api)),
        loading: true,
        error: undefined,
      }));
      void api
        .listClassifierEventPage(
          {
            pageToken,
            afterEventSequence: "0",
            beforeEventSequence,
            pageSize: DESCRIPTOR_PAGE_SIZE,
            order: DescriptorPageOrder.NEWEST_FIRST,
            expectedOperatorInstanceId: operatorInstanceId,
            expectedAsOfEventUlid: asOfEventUlid,
            expectedRunId: runId,
            expectedNodeId: nodeId,
          },
          controller.signal,
        )
        .then((page) => {
          if (controller.signal.aborted) return;
          setStored((current) => {
            if (current.api !== api) return current;
            const grouped = groupInvocations([...current.page.records, ...page.records]);
            return {
              ...current,
              loaded: true,
              // Once newer groups leave this window, an older start cannot prove
              // interruption: its terminal record may have been among the evictions.
              newerHistoryEvicted:
                current.newerHistoryEvicted || grouped.size > DESCRIPTOR_WINDOW_SIZE,
              page: {
                ...page,
                records: boundDescriptors(grouped, (event) => event.eventSequence, "older"),
              },
            };
          });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setStored((current) =>
            current.api !== api
              ? current
              : {
                  ...current,
                  error:
                    error instanceof Error ? error.message : "Classifier history unavailable",
                },
          );
        })
        .finally(() => {
          if (controller.signal.aborted) return;
          pageController.current = undefined;
          setStored((current) =>
            current.api !== api ? current : { ...current, loading: false },
          );
        });
    },
    [api, asOfEventUlid, nodeId, operatorInstanceId, runId],
  );

  useEffect(() => {
    setStored(emptyHistory(api));
    if (eventPageToken && runId) loadPage(eventPageToken);
    else setStored({ ...emptyHistory(api), loaded: true });
    const controllers = detailControllers.current;
    return () => {
      pageController.current?.abort();
      pageController.current = undefined;
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
    };
  }, [api, eventPageToken, loadPage, runId]);

  const events = useMemo(() => {
    const grouped = groupInvocations([...state.page.records, ...liveEvents]);
    const liveSequences = liveEvents.map((event) => event.eventSequence);
    return boundDescriptors(grouped, (event) => event.eventSequence, "older", liveSequences);
  }, [liveEvents, state.page.records]);
  const newestFirstEvents = useMemo(() => events.toReversed(), [events]);
  const selectedId =
    selection?.api === api &&
    (selection.invocationId === null ||
      events.some((event) => event.invocationId === selection.invocationId))
      ? selection.invocationId
      : events.at(-1)?.invocationId;

  // Only descriptors in the visible window may retain request/error/cache state.
  useEffect(() => {
    const tokens = new Set(events.map((event) => event.bodyToken));
    for (const [token, controller] of detailControllers.current) {
      if (!tokens.has(token)) {
        controller.abort();
        detailControllers.current.delete(token);
      }
    }
    setStored((current) => {
      if (
        current.api !== api ||
        [...current.details.keys()].every((token) => tokens.has(token))
      )
        return current;
      return {
        ...current,
        details: new Map([...current.details].filter(([token]) => tokens.has(token))),
        cache: new Map([...current.cache].filter(([token]) => tokens.has(token))),
      };
    });
  }, [api, events]);

  const hydrate = useCallback(
    (event: ClassifierEventDescriptorMsg) => {
      if (!event.bodyToken || detailControllers.current.has(event.bodyToken)) return;
      const token = event.bodyToken;
      const controller = new AbortController();
      detailControllers.current.set(token, controller);
      setStored((current) =>
        current.api !== api
          ? current
          : {
              ...current,
              details: new Map(current.details).set(token, { status: "loading" }),
            },
      );
      void api
        .readJsonDetail(token, controller.signal)
        .then((body) => {
          if (controller.signal.aborted) return;
          const invocation = parseClassifierInvocation(body);
          if (
            invocation.invocation_id !== event.invocationId ||
            invocation.status !== event.eventKind
          ) {
            throw new Error("Classifier detail does not match its invocation descriptor");
          }
          const byteCost = measuredByteCost(body, event.sizeBytes);
          setStored((current) => {
            if (current.api !== api) return current;
            const details = new Map(current.details);
            if (byteCost > DETAIL_CACHE_MAX_BYTES) {
              details.set(token, {
                status: "error",
                error: "Invocation detail exceeds the browser detail limit.",
                retryable: false,
              });
              return { ...current, details };
            }
            const cache = new Map(current.cache);
            cache.delete(token);
            cache.set(token, {
              invocation,
              byteCost,
              inputPreview: summarizeInput(invocation.input),
            });
            details.set(token, { status: "ready" });
            let bytes = 0;
            for (const entry of cache.values()) bytes += entry.byteCost;
            while (cache.size > DETAIL_CACHE_MAX_ENTRIES || bytes > DETAIL_CACHE_MAX_BYTES) {
              const oldest = cache.entries().next().value;
              if (!oldest) break;
              cache.delete(oldest[0]);
              bytes -= oldest[1].byteCost;
              details.set(oldest[0], { status: "released" });
            }
            return { ...current, cache, details };
          });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setStored((current) =>
            current.api !== api
              ? current
              : {
                  ...current,
                  details: new Map(current.details).set(token, {
                    status: "error",
                    error:
                      error instanceof Error ? error.message : "Invocation detail unavailable",
                    retryable: true,
                  }),
                },
          );
        })
        .finally(() => {
          if (!controller.signal.aborted) detailControllers.current.delete(token);
        });
    },
    [api],
  );

  useEffect(() => {
    for (const event of events.slice(-DETAIL_CACHE_MAX_ENTRIES)) {
      if (!state.details.has(event.bodyToken)) hydrate(event);
    }
  }, [events, hydrate, state.details]);

  useEffect(() => {
    const selected = events.find((event) => event.invocationId === selectedId);
    if (selected && !state.details.has(selected.bodyToken)) hydrate(selected);
  }, [events, hydrate, selectedId, state.details]);

  const terminalNode =
    node?.status === "failed" || node?.status === "cancelled" || node?.status === "success";
  const terminalRun =
    run.summary?.status === "failed" ||
    run.summary?.status === "cancelled" ||
    run.summary?.status === "success";
  return (
    <section aria-label="Classifier invocations" className="grid min-w-0 gap-4">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="inspector-section-title">Calls</h3>
        <span className="font-mono text-[10px] text-muted">{events.length} loaded</span>
      </div>
      {state.loading && (
        <p role="status" className="m-0 text-[11px] text-muted">
          Loading classifier history…
        </p>
      )}
      {state.error && (
        <div
          role="alert"
          className="grid min-w-0 gap-2 text-[11px] text-danger [overflow-wrap:anywhere]"
        >
          <p className="m-0">Classifier history unavailable: {state.error}</p>
          <button
            className={BUTTON_CLASS}
            type="button"
            disabled={state.loading}
            onClick={() =>
              loadPage(
                state.loaded ? state.page.nextPageToken : eventPageToken,
                state.loaded ? state.page.nextCursor : "0",
              )
            }
          >
            Retry classifier history
          </button>
        </div>
      )}
      {state.loaded && !state.error && !events.length && (
        <p role="status" className="m-0 text-[11px] text-muted">
          {node
            ? "Classifier not invoked in this run."
            : "This node has no execution data yet."}
        </p>
      )}
      {newestFirstEvents.map((event) => {
        const cached = state.cache.get(event.bodyToken);
        const invocation = cached?.invocation;
        const detail = state.details.get(event.bodyToken);
        const selected = event.invocationId === selectedId;
        const ended = terminalNode || terminalRun;
        const unresolved =
          event.eventKind === "running" &&
          (state.newerHistoryEvicted || (ended && !state.loaded));
        const interrupted = event.eventKind === "running" && ended && !unresolved;
        const status = unresolved ? "unknown" : interrupted ? "interrupted" : event.eventKind;
        const statusColor =
          status === "success"
            ? "text-success"
            : status === "failed"
              ? "text-danger"
              : status === "running" || status === "unknown" || status === "interrupted"
                ? "text-amber"
                : "text-muted";
        return (
          <article
            key={event.invocationId}
            aria-label={`Invocation ${event.invocationId}`}
            className={`grid min-w-0 overflow-hidden rounded-lg border ${selected ? "border-classifier" : "border-line"}`}
          >
            <button
              type="button"
              aria-expanded={selected}
              className={`flex min-w-0 cursor-pointer items-start gap-2 border-0 p-3 text-left focus-visible:outline-2 focus-visible:outline-classifier ${selected ? "bg-classifier-light" : "bg-panel hover:bg-canvas"}`}
              onClick={() =>
                setSelection({
                  api,
                  invocationId: selected ? null : event.invocationId,
                })
              }
            >
              {selected ? (
                <ChevronDown
                  aria-hidden="true"
                  className="mt-0.5 size-3.5 shrink-0 text-classifier"
                />
              ) : (
                <ChevronRight
                  aria-hidden="true"
                  className="mt-0.5 size-3.5 shrink-0 text-muted"
                />
              )}
              <span className="grid min-w-0 flex-1 gap-1">
                <span className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-xs font-semibold text-ink">
                    {invocation ? `Call ${invocation.invocation_index + 1}` : "Call"}
                  </span>
                  <span
                    role="status"
                    aria-label="Invocation status"
                    className={`font-mono text-[10px] capitalize ${statusColor}`}
                  >
                    {status}
                  </span>
                </span>
                {invocation && (
                  <span className="font-mono text-[10px] text-secondary">
                    <time>{new Date(invocation.started_at * 1000).toLocaleTimeString()}</time>
                    {invocation.ended_at !== null &&
                      ` · ${(invocation.ended_at - invocation.started_at).toFixed(2)}s`}
                  </span>
                )}
                {cached && (
                  <span className="truncate text-[11px] text-secondary">
                    {cached.inputPreview}
                  </span>
                )}
                <span className="font-mono text-[9px] text-muted [overflow-wrap:anywhere]">
                  {event.invocationId}
                </span>
              </span>
            </button>
            {selected && (
              <div className="grid min-w-0 gap-3 border-t border-line p-3">
                {unresolved && (
                  <p role="status" className="m-0 text-[11px] text-amber">
                    {state.newerHistoryEvicted
                      ? "Latest invocation result not loaded. Newer history has left this browser's window; return to latest invocations to inspect retained terminal evidence."
                      : "Terminal invocation result not loaded. Waiting for classifier history."}
                  </p>
                )}
                {interrupted && (
                  <p role="status" className="m-0 text-[11px] text-amber">
                    Invocation interrupted. The node or run ended before terminal classifier
                    evidence was retained; answers are unavailable.
                  </p>
                )}
                {invocation && (
                  <>
                    <ClassifierInvocationDetails
                      invocation={invocation}
                      statusOverride={
                        unresolved ? "unknown" : interrupted ? "interrupted" : undefined
                      }
                    />
                    <details className="min-w-0">
                      <summary className="cursor-pointer text-[11px] text-secondary">
                        Questions for this invocation
                      </summary>
                      <div className="mt-3 min-w-0">
                        <ClassifierQuestions declaration={invocation.declaration} />
                      </div>
                    </details>
                  </>
                )}
                {!invocation &&
                  (!event.bodyToken ? (
                    <p className="m-0 text-[11px] text-muted">
                      Invocation detail unavailable: no retained body.
                    </p>
                  ) : detail?.status === "loading" ? (
                    <p role="status" className="m-0 text-[11px] text-muted">
                      Loading invocation details…
                    </p>
                  ) : detail?.status === "error" ? (
                    <div className="grid min-w-0 gap-2">
                      <p
                        role="alert"
                        className="m-0 text-[11px] text-danger [overflow-wrap:anywhere]"
                      >
                        Invocation detail unavailable: {detail.error}
                      </p>
                      {detail.retryable && (
                        <button
                          className={BUTTON_CLASS}
                          type="button"
                          onClick={() => hydrate(event)}
                        >
                          Retry invocation details
                        </button>
                      )}
                    </div>
                  ) : (
                    <div className="grid min-w-0 gap-2">
                      {detail?.status === "released" && (
                        <p className="m-0 text-[11px] text-muted">
                          Invocation details released from the browser cache.
                        </p>
                      )}
                      <button
                        className={BUTTON_CLASS}
                        type="button"
                        onClick={() => hydrate(event)}
                      >
                        {detail?.status === "released"
                          ? "Reload invocation details"
                          : "Load invocation details"}
                      </button>
                    </div>
                  ))}
              </div>
            )}
          </article>
        );
      })}
      {events.length === DESCRIPTOR_WINDOW_SIZE && (
        <p className="m-0 text-[11px] text-muted">
          Only a bounded window of invocation history is held in this browser.
        </p>
      )}
      {state.page.nextPageToken && !state.error && (
        <button
          className={BUTTON_CLASS}
          type="button"
          disabled={state.loading}
          onClick={() => loadPage(state.page.nextPageToken, state.page.nextCursor)}
        >
          Load earlier invocations
        </button>
      )}
      {state.page.records.length > 0 && (
        <button
          className={BUTTON_CLASS}
          type="button"
          disabled={state.loading}
          onClick={() => {
            for (const controller of detailControllers.current.values()) controller.abort();
            detailControllers.current.clear();
            setSelection(undefined);
            setStored(emptyHistory(api));
            loadPage(eventPageToken);
          }}
        >
          Return to latest invocations
        </button>
      )}
    </section>
  );
}

type ClassifierTab = "definition" | "code" | "calls";
const WORKFLOW_TABS: ClassifierTab[] = ["definition", "code"];
const RUN_TABS: ClassifierTab[] = ["calls", "definition"];

type SourceState = {
  api: OperatorApi;
  workflow: FlowInfoMsg;
  nodeId: string;
} & (
  | { status: "loading" }
  | { status: "ready"; source: string | undefined }
  | { status: "error"; error: string }
);

function ClassifierSource({
  api,
  workflow,
  nodeId,
}: {
  api: OperatorApi;
  workflow: FlowInfoMsg;
  nodeId: string;
}) {
  const [stored, setStored] = useState<SourceState>();
  const [attempt, setAttempt] = useState(0);
  const state =
    stored?.api === api && stored.workflow === workflow && stored.nodeId === nodeId
      ? stored
      : undefined;
  useEffect(() => {
    const controller = new AbortController();
    const scope = { api, workflow, nodeId };
    setStored({ ...scope, status: "loading" });
    void api
      .getWorkflowNodeSource(workflow.name, nodeId, controller.signal)
      .then((source) => {
        if (!controller.signal.aborted) setStored({ ...scope, status: "ready", source });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setStored({
            ...scope,
            status: "error",
            error: error instanceof Error ? error.message : "Source code unavailable",
          });
      });
    return () => controller.abort();
  }, [api, attempt, nodeId, workflow]);
  if (state?.status === "ready")
    return state.source !== undefined ? (
      <PythonSource source={state.source} />
    ) : (
      <p className="px-5 text-[11px] text-muted">Source code is unavailable for this node.</p>
    );
  if (state?.status === "error")
    return (
      <div className="grid gap-2 p-5">
        <p role="alert" className="m-0 text-[11px] text-danger [overflow-wrap:anywhere]">
          Source code is unavailable: {state.error}
        </p>
        <button type="button" className={BUTTON_CLASS} onClick={() => setAttempt(attempt + 1)}>
          Retry source code
        </button>
      </div>
    );
  return (
    <p role="status" className="px-5 text-[11px] text-muted">
      Loading source code…
    </p>
  );
}

export function ClassifierInspector({
  api,
  nodeId,
  workflow,
  run,
  declaration,
  declarationError,
  liveEvents = EMPTY_EVENTS,
  embedded = false,
  definitionLabel = "Current definition",
  onClose,
}: ClassifierInspectorProps) {
  const node = run?.nodes.find((item) => item.nodeId === nodeId);
  const runId = run?.summary?.runId;
  const scope = `${run?.operatorInstanceId ?? ""}\0${runId ?? ""}\0${nodeId}\0${run?.asOfEventUlid ?? ""}\0${node?.eventPageToken ?? ""}`;
  const tabsId = useId();
  const tabScope = run ? `run\0${scope}` : `workflow\0${workflow?.name ?? ""}\0${nodeId}`;
  const [selectedTab, setSelectedTab] = useState<{ scope: string; tab: ClassifierTab }>();
  const tabs = run ? RUN_TABS : WORKFLOW_TABS;
  const tab = selectedTab?.scope === tabScope ? selectedTab.tab : run ? "calls" : "definition";
  const name = run
    ? run.topology?.displayNames[nodeId] || node?.name || nodeId
    : workflow?.displayNames[nodeId] || nodeId;
  const panelLayout = embedded
    ? "relative h-full w-full"
    : "fixed right-0 bottom-0 top-[58px] h-auto w-[min(var(--workspace-inspector-width),100vw)] shadow-[-20px_0_50px_rgba(20,31,26,.14)] max-[700px]:w-screen min-[1001px]:static min-[1001px]:z-auto min-[1001px]:h-full min-[1001px]:w-auto min-[1001px]:shadow-none";
  return (
    <aside
      className={`inspector ${run ? "inspector-run" : "inspector-declaration"} z-30 flex min-h-0 min-w-0 flex-col overflow-hidden border-l border-line bg-panel ${panelLayout}`}
      aria-label={run ? "Run classifier inspector" : "Classifier declaration"}
    >
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-5 pt-[19px] pb-3.5">
        <div className="min-w-0 flex-1">
          <span className="eyebrow flex items-center gap-1.5 font-mono text-[9px] tracking-[.16em] text-classifier uppercase">
            <ListFilter aria-hidden="true" className="size-3.5" strokeWidth={1.8} />
            Classifier · {run ? `Run ${runId ?? ""}` : "Workflow"}
          </span>
          <h2 className="mt-1 mb-0 min-w-0 text-lg [overflow-wrap:anywhere]">{name}</h2>
          {node && (
            <span className="font-mono text-[9px] text-muted uppercase">{node.status}</span>
          )}
          {node?.error && (
            <p
              role="alert"
              className="mt-2 mb-0 text-[11px] text-danger [overflow-wrap:anywhere]"
            >
              {node.error}
            </p>
          )}
        </div>
        <button
          type="button"
          className="icon-button grid size-[30px] shrink-0 cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-acid"
          onClick={onClose}
          aria-label="Close"
        >
          <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
        </button>
      </header>
      <div
        className="inspector-tabs flex shrink-0 border-b border-line px-2.5"
        role="tablist"
        aria-label={run ? "Run classifier views" : "Workflow classifier views"}
      >
        {tabs.map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            id={`${tabsId}-${item}`}
            aria-controls={`${tabsId}-panel`}
            aria-selected={tab === item}
            tabIndex={tab === item ? 0 : -1}
            className={`flex-1 cursor-pointer border-0 border-b-2 bg-transparent px-[9px] pt-[11px] pb-[9px] font-mono text-[9px] uppercase focus-visible:outline-2 focus-visible:outline-classifier ${tab === item ? "border-classifier text-classifier" : "border-transparent text-muted"}`}
            onClick={() => setSelectedTab({ scope: tabScope, tab: item })}
            onKeyDown={(event) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              const next =
                event.key === "Home"
                  ? tabs[0]
                  : event.key === "End"
                    ? tabs[tabs.length - 1]
                    : tabs[(tabs.indexOf(item) + 1) % tabs.length];
              setSelectedTab({ scope: tabScope, tab: next });
              document.getElementById(`${tabsId}-${next}`)?.focus();
            }}
          >
            {item === "definition" ? "Definition" : item === "code" ? "Code" : "Calls"}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`${tabsId}-panel`}
        aria-labelledby={`${tabsId}-${tab}`}
        className={`inspector-body min-h-0 min-w-0 flex-1 ${tab === "code" ? "overflow-hidden bg-canvas" : "overflow-auto px-5 pt-[18px] pb-[30px] [scrollbar-gutter:stable]"}`}
      >
        {tab === "definition" && (
          <section
            aria-label={run ? "Historical classifier declaration" : definitionLabel}
            className="min-w-0"
          >
            {run && (
              <p className="mt-0 text-[11px] text-muted">
                Retained from this run, not the current workflow definition.
              </p>
            )}
            {declaration ? (
              <ClassifierQuestions declaration={declaration} />
            ) : (
              <p role="alert" className="text-[11px] text-danger [overflow-wrap:anywhere]">
                {run
                  ? "Historical classifier declaration unavailable."
                  : "Classifier declaration unavailable."}{" "}
                {declarationError}
              </p>
            )}
          </section>
        )}
        {tab === "code" && !run && workflow && (
          <ClassifierSource api={api} workflow={workflow} nodeId={nodeId} />
        )}
        {run && (
          <div hidden={tab !== "calls"}>
            <ClassifierHistory
              key={scope}
              api={api}
              run={run}
              node={node}
              nodeId={nodeId}
              liveEvents={liveEvents}
            />
          </div>
        )}
      </div>
    </aside>
  );
}
