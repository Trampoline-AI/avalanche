import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, ListFilter, X } from "lucide-react";

import type { OperatorApi } from "./api";
import { ClassifierInvocationDetails, ClassifierQuestions } from "./ClassifierDetails";
import {
  type ClassifierDeclaration,
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
const CALL_PAGE_SIZE = 25;
const PAGE_BUTTON_CLASS =
  "cursor-pointer rounded-md border border-line bg-transparent px-3 py-2 text-ink hover:bg-canvas focus-visible:outline-2 focus-visible:outline-classifier disabled:cursor-not-allowed disabled:opacity-40";
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

interface CachedInvocation {
  invocation: ClassifierInvocation;
  byteCost: number;
}
type DetailStatus =
  { status: "loading" | "ready" } | { status: "error"; error: string; retryable: boolean };
interface HistoryState {
  api: OperatorApi;
  page: DescriptorPageState<ClassifierEventDescriptorMsg>;
  loaded: boolean;
  loading: boolean;
  historyEvicted: boolean;
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
    historyEvicted: false,
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
  const [pageStartId, setPageStartId] = useState<string | null>(null);
  const [nextPageAfter, setNextPageAfter] = useState<string | null>(null);
  const historyId = useId();
  const tableScroll = useRef<HTMLDivElement>(null);
  const [stored, setStored] = useState(() => emptyHistory(api));
  const state = stored.api === api ? stored : emptyHistory(api);
  const pageController = useRef<AbortController | undefined>(undefined);
  const detailControllers = useRef(new Map<string, AbortController>());
  const selectedBodyToken = useRef<string | undefined>(undefined);
  const runId = run.summary?.runId ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const operatorInstanceId = run.operatorInstanceId;
  const asOfEventUlid = run.asOfEventUlid;

  const loadPage = useCallback(
    (pageToken: string, afterEventSequence = "0") => {
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
            afterEventSequence,
            beforeEventSequence: "0",
            pageSize: DESCRIPTOR_PAGE_SIZE,
            order: DescriptorPageOrder.FORWARD,
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
              historyEvicted: current.historyEvicted || grouped.size > DESCRIPTOR_WINDOW_SIZE,
              page: {
                ...page,
                records: boundDescriptors(grouped, (event) => event.eventSequence, "newer"),
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
    setSelection(undefined);
    setPageStartId(null);
    setNextPageAfter(null);
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
    return boundDescriptors(grouped, (event) => String(event.invocationIndex), "older");
  }, [liveEvents, state.page.records]);
  const pageOffset =
    pageStartId === null
      ? 0
      : Math.max(
          0,
          events.findIndex((event) => event.invocationId === pageStartId),
        );
  const visibleEvents = events.slice(pageOffset, pageOffset + CALL_PAGE_SIZE);
  const canAdvance =
    pageOffset + CALL_PAGE_SIZE < events.length || Boolean(state.page.nextPageToken);

  useEffect(() => {
    if (!state.loaded || state.loading || state.error) return;
    const afterIndex =
      nextPageAfter === null
        ? pageOffset - 1
        : events.findIndex((event) => event.invocationId === nextPageAfter);
    const available = events.length - afterIndex - 1;
    if (available < CALL_PAGE_SIZE && state.page.nextPageToken) {
      loadPage(state.page.nextPageToken, state.page.nextCursor);
    } else if (nextPageAfter !== null) {
      if (available > 0) {
        setPageStartId(events[afterIndex + 1].invocationId);
      }
      setNextPageAfter(null);
    }
  }, [
    loadPage,
    events,
    nextPageAfter,
    pageOffset,
    state.error,
    state.loaded,
    state.loading,
    state.page.nextCursor,
    state.page.nextPageToken,
  ]);

  useEffect(() => {
    if (tableScroll.current) tableScroll.current.scrollTop = 0;
  }, [pageStartId]);
  const selectedId =
    selection?.api === api &&
    (selection.invocationId === null ||
      visibleEvents.some((event) => event.invocationId === selection.invocationId))
      ? selection.invocationId
      : null;

  useEffect(() => {
    selectedBodyToken.current = events.find(
      (event) => event.invocationId === selectedId,
    )?.bodyToken;
  }, [events, selectedId]);

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
            invocation.invocation_index !== event.invocationIndex ||
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
            });
            details.set(token, { status: "ready" });
            let bytes = 0;
            for (const entry of cache.values()) bytes += entry.byteCost;
            for (const [key, entry] of cache) {
              if (cache.size <= DETAIL_CACHE_MAX_ENTRIES && bytes <= DETAIL_CACHE_MAX_BYTES)
                break;
              if (key === selectedBodyToken.current) continue;
              cache.delete(key);
              bytes -= entry.byteCost;
              details.delete(key);
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
    <section
      aria-label="Classifier invocations"
      className="flex h-full min-h-0 min-w-0 flex-col"
    >
      {state.loading && (
        <p role="status" className="m-0 shrink-0 px-3 py-2 text-[11px] text-muted">
          Loading classifier history…
        </p>
      )}
      {state.error && (
        <div
          role="alert"
          className="grid min-w-0 shrink-0 gap-2 px-3 py-2 text-[11px] text-danger [overflow-wrap:anywhere]"
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
        <p role="status" className="m-0 px-3 py-4 text-[11px] text-muted">
          {node
            ? "Classifier not invoked in this run."
            : "This node has no execution data yet."}
        </p>
      )}
      <div ref={tableScroll} className="min-h-0 flex-1 overflow-auto overscroll-contain">
        <table
          aria-label="Classifier calls"
          className="w-full table-fixed border-collapse text-left text-[11px]"
        >
          <colgroup>
            <col className="w-16" />
            <col />
            <col className="w-[4.5rem]" />
            <col className="w-16" />
          </colgroup>
          <thead className="sticky top-0 z-10 bg-canvas font-mono text-[9px] text-muted uppercase">
            <tr className="border-b border-line">
              <th scope="col" className="px-3 py-2 font-normal">
                Call
              </th>
              <th scope="col" className="px-2 py-2 font-normal">
                Outputs
              </th>
              <th scope="col" className="px-2 py-2 font-normal">
                Status
              </th>
              <th scope="col" className="px-3 py-2 text-right font-normal">
                Duration
              </th>
            </tr>
          </thead>
          {visibleEvents.map((event) => {
            const invocation = state.cache.get(event.bodyToken)?.invocation;
            const detail = state.details.get(event.bodyToken);
            const selected = event.invocationId === selectedId;
            const ended = terminalNode || terminalRun;
            const unresolved =
              event.eventKind === "running" &&
              (state.historyEvicted ||
                (ended && (!state.loaded || Boolean(state.page.nextPageToken))));
            const interrupted = event.eventKind === "running" && ended && !unresolved;
            const status = unresolved
              ? "unknown"
              : interrupted
                ? "interrupted"
                : event.eventKind;
            const statusColor =
              status === "success"
                ? "text-success"
                : status === "failed"
                  ? "text-danger"
                  : status === "running" || status === "unknown" || status === "interrupted"
                    ? "text-amber"
                    : "text-muted";
            const detailsId = `${historyId}-${event.eventSequence}`;
            return (
              <tbody key={event.invocationId} aria-label={`Invocation ${event.invocationId}`}>
                <tr
                  onClick={() =>
                    setSelection({ api, invocationId: selected ? null : event.invocationId })
                  }
                  className={`cursor-pointer align-top ${selected ? "" : "border-b border-[#eef1ef] hover:bg-canvas"}`}
                >
                  <th scope="row" className="px-3 py-2.5 font-normal">
                    <button
                      type="button"
                      aria-expanded={selected}
                      aria-controls={detailsId}
                      aria-label={`Call ${event.invocationIndex + 1}`}
                      className="flex cursor-pointer items-center gap-1.5 rounded border-0 bg-transparent p-0 text-[11px] font-medium whitespace-nowrap text-ink focus-visible:outline-2 focus-visible:outline-classifier"
                    >
                      {selected ? (
                        <ChevronDown
                          aria-hidden="true"
                          className="size-3 shrink-0 text-classifier"
                        />
                      ) : (
                        <ChevronRight
                          aria-hidden="true"
                          className="size-3 shrink-0 text-muted"
                        />
                      )}
                      {event.invocationIndex + 1}
                    </button>
                  </th>
                  <td className="px-2 py-2.5 leading-relaxed text-secondary [overflow-wrap:anywhere]">
                    {event.answers.length > 0 ? (
                      event.answers.map((answer, index) => (
                        <span key={answer.questionId}>
                          {index > 0 && <span className="text-muted"> · </span>}
                          <span className="text-muted">{answer.questionId}: </span>
                          <span className="text-classifier">
                            {answer.type === "choice"
                              ? answer.choice
                              : answer.type === "noul"
                                ? answer.noul
                                : answer.score}
                          </span>
                        </span>
                      ))
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="px-2 py-2.5">
                    <span
                      role="status"
                      aria-label="Invocation status"
                      className={`text-[10px] capitalize ${statusColor}`}
                    >
                      {status}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-[10px] whitespace-nowrap text-secondary">
                    {event.durationMs === undefined
                      ? "—"
                      : `${(Number(event.durationMs) / 1000).toFixed(1)}s`}
                  </td>
                </tr>
                {selected && (
                  <tr className="border-b border-line">
                    <td colSpan={4} className="p-0">
                      <div id={detailsId} className="grid min-w-0 gap-3 pt-1 pr-3 pb-3 pl-6">
                        {unresolved && !invocation && (
                          <p role="status" className="m-0 text-[11px] text-amber">
                            Final result not loaded in this history window.
                          </p>
                        )}
                        {interrupted && !invocation && (
                          <p role="status" className="m-0 text-[11px] text-amber">
                            Call interrupted; no output retained.
                          </p>
                        )}
                        {invocation && (
                          <ClassifierInvocationDetails
                            invocation={invocation}
                            statusOverride={
                              unresolved ? "unknown" : interrupted ? "interrupted" : undefined
                            }
                          />
                        )}
                        {!invocation &&
                          (!event.bodyToken ? (
                            <p className="m-0 text-[11px] text-muted">
                              Call detail unavailable.
                            </p>
                          ) : detail?.status === "error" ? (
                            <div className="grid min-w-0 gap-2">
                              <p
                                role="alert"
                                className="m-0 text-[11px] text-danger [overflow-wrap:anywhere]"
                              >
                                {detail.error}
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
                            <p role="status" className="m-0 text-[11px] text-muted">
                              Loading…
                            </p>
                          ))}
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            );
          })}
        </table>
      </div>
      {events.length === DESCRIPTOR_WINDOW_SIZE && (
        <p className="m-0 shrink-0 px-3 py-2 text-[10px] text-muted">
          Only a bounded window of invocation history is held in this browser.
        </p>
      )}
      <nav
        aria-label="Call pagination"
        className="flex shrink-0 flex-col gap-3 border-t border-[#eef1ef] px-3 py-3 text-xs text-secondary"
      >
        <span role="status">
          {events.length
            ? `${pageOffset + 1}–${pageOffset + visibleEvents.length} of ${events.length}${state.page.nextPageToken ? "+" : ""} calls`
            : "0 calls"}
        </span>
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            aria-label="Previous page"
            className={PAGE_BUTTON_CLASS}
            disabled={pageOffset === 0 || state.loading || nextPageAfter !== null}
            onClick={() => {
              const offset = Math.max(0, pageOffset - CALL_PAGE_SIZE);
              setPageStartId(offset < CALL_PAGE_SIZE ? null : events[offset].invocationId);
              setSelection({ api, invocationId: null });
            }}
          >
            Previous
          </button>
          <span>Page {Math.floor(pageOffset / CALL_PAGE_SIZE) + 1}</span>
          <button
            type="button"
            aria-label="Next page"
            className={PAGE_BUTTON_CLASS}
            disabled={
              !canAdvance || state.loading || nextPageAfter !== null || Boolean(state.error)
            }
            onClick={() => {
              setSelection({ api, invocationId: null });
              setNextPageAfter(visibleEvents[visibleEvents.length - 1].invocationId);
            }}
          >
            Next
          </button>
        </div>
        {state.historyEvicted && (
          <button
            className={BUTTON_CLASS}
            type="button"
            disabled={state.loading}
            onClick={() => {
              for (const controller of detailControllers.current.values()) controller.abort();
              detailControllers.current.clear();
              setPageStartId(null);
              setNextPageAfter(null);
              setSelection(undefined);
              setStored(emptyHistory(api));
              loadPage(eventPageToken);
            }}
          >
            Return to first invocations
          </button>
        )}
      </nav>
    </section>
  );
}

type ClassifierTab = "definition" | "code";
const WORKFLOW_TABS: ClassifierTab[] = ["definition", "code"];

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
  const tabScope = `workflow\0${workflow?.name ?? ""}\0${nodeId}`;
  const [selectedTab, setSelectedTab] = useState<{ scope: string; tab: ClassifierTab }>();
  const tab = selectedTab?.scope === tabScope ? selectedTab.tab : "definition";
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
            Classifier · {run ? "Run" : "Workflow"}
          </span>
          <h2 className="mt-1 mb-0 min-w-0 text-lg [overflow-wrap:anywhere]">{name}</h2>
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
      {!run && (
        <div
          className="inspector-tabs flex shrink-0 border-b border-line px-2.5"
          role="tablist"
          aria-label="Workflow classifier views"
        >
          {WORKFLOW_TABS.map((item) => (
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
                    ? WORKFLOW_TABS[0]
                    : event.key === "End"
                      ? WORKFLOW_TABS[WORKFLOW_TABS.length - 1]
                      : WORKFLOW_TABS[(WORKFLOW_TABS.indexOf(item) + 1) % WORKFLOW_TABS.length];
                setSelectedTab({ scope: tabScope, tab: next });
                document.getElementById(`${tabsId}-${next}`)?.focus();
              }}
            >
              {item === "definition" ? "Definition" : "Code"}
            </button>
          ))}
        </div>
      )}
      {run ? (
        <div className="inspector-body min-h-0 min-w-0 flex-1 overflow-hidden">
          <ClassifierHistory
            key={scope}
            api={api}
            run={run}
            node={node}
            nodeId={nodeId}
            liveEvents={liveEvents}
          />
        </div>
      ) : (
        <div
          role="tabpanel"
          id={`${tabsId}-panel`}
          aria-labelledby={`${tabsId}-${tab}`}
          className={`inspector-body min-h-0 min-w-0 flex-1 ${tab === "code" ? "overflow-hidden bg-canvas" : "overflow-auto px-3 py-1 [scrollbar-gutter:stable]"}`}
        >
          {tab === "definition" && (
            <section aria-label={definitionLabel} className="min-w-0">
              {declaration ? (
                <ClassifierQuestions declaration={declaration} />
              ) : (
                <p role="alert" className="text-[11px] text-danger [overflow-wrap:anywhere]">
                  Classifier declaration unavailable. {declarationError}
                </p>
              )}
            </section>
          )}
          {tab === "code" && workflow && (
            <ClassifierSource api={api} workflow={workflow} nodeId={nodeId} />
          )}
        </div>
      )}
    </aside>
  );
}
