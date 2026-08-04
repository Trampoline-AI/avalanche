import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { OperatorApi } from "./api";
import {
  boundDescriptors,
  DESCRIPTOR_PAGE_SIZE,
  DETAIL_CACHE_MAX_BYTES,
  type DescriptorPageState,
  measuredByteCost,
  mergeDescriptorPage,
  SCROLL_LOAD_THRESHOLD_PX,
} from "./detailProjection";
import { parseAgentDeclaration, parseAgentFieldSchemas } from "./GraphCanvas";
import {
  DescriptorPageOrder,
  type AgentEventDescriptorMsg,
  type FlowInfoMsg,
  type NodeSnapshotMsg,
  type RunSnapshotMsg,
} from "./generated/operator";
import { isUnknownRecord } from "./guards";
import { Markdown } from "./Markdown";
import { ValueView } from "./ValueView";

interface InspectorProps {
  api: OperatorApi;
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  nodeId?: string;
  liveEvents?: AgentEventDescriptorMsg[];
  onClose: () => void;
}

type RunTab = "overview" | "inputs" | "output" | "trace";
type DetailFormat = "json";


interface ScopedResult<T> {
  key: string;
  value: T;
}

interface DetailCacheEntry {
  value: unknown;
  byteCost: number;
}

interface InputOutputState {
  key: string;
  status: "loading" | "ready" | "error";
  error?: string;
}

const DETAIL_CACHE_MAX_ENTRIES = 8;

const EMPTY_EVENTS: AgentEventDescriptorMsg[] = [];
const EMPTY_EVENT_PAGE: DescriptorPageState<AgentEventDescriptorMsg> = {
  records: EMPTY_EVENTS,
  nextPageToken: "",
  nextCursor: "0",
};


function parseRetainedJson(value: string | undefined) {
  if (!value) return undefined;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

function eventPayload(value: unknown): Record<string, unknown> | undefined {
  if (!isUnknownRecord(value)) return undefined;
  return isUnknownRecord(value.data) ? value.data : value;
}

function eventSummary(event: AgentEventDescriptorMsg) {
  return {
    event: event.eventKind,
    event_sequence: event.eventSequence,
    invocation_id: event.invocationId,
    iteration: event.iteration,
    duration_ms: event.durationMs,
    tool_count: event.toolCount,
    predict_count: event.predictCount,
    failed: event.error,
  };
}

export function Inspector({
  api,
  workflow,
  run,
  nodeId,
  liveEvents = EMPTY_EVENTS,
  onClose,
}: InspectorProps) {
  const [tabSelection, setTabSelection] = useState<{ scope: string; tab: RunTab }>();
  const [eventPage, setEventPage] =
    useState<DescriptorPageState<AgentEventDescriptorMsg>>(EMPTY_EVENT_PAGE);
  const [eventPageScope, setEventPageScope] = useState<string>();
  const [pageError, setPageError] = useState<ScopedResult<string>>();
  const [pageLoading, setPageLoading] = useState(false);
  const [following, setFollowing] = useState(true);
  const [cacheVersion, setCacheVersion] = useState(0);
  const [detailErrors, setDetailErrors] = useState<Record<string, string>>({});
  const [detailLoadingVersion, setDetailLoadingVersion] = useState(0);
  const [inputOutputState, setInputOutputState] = useState<InputOutputState>();

  const detailCache = useRef(new Map<string, DetailCacheEntry>());
  const detailLoading = useRef(new Set<string>());
  const detailControllers = useRef(new Set<AbortController>());
  const pageController = useRef<AbortController | null>(null);
  const pageRequestInFlight = useRef(false);
  const pageGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const traceScrollElement = useRef<HTMLDivElement>(null);
  const tabRef = useRef<RunTab>("overview");

  const node: NodeSnapshotMsg | undefined = run?.nodes.find((item) => item.nodeId === nodeId);
  const runId = run?.summary?.runId;
  const operatorInstanceId = run?.operatorInstanceId ?? "";
  const asOfSequence = run?.asOfSequence ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const hasRunNode = Boolean(run && node);
  const selectionScope = `${operatorInstanceId}\0${runId ?? ""}\0${nodeId ?? ""}`;
  const descriptorScope = `${selectionScope}\0${asOfSequence}\0${eventPageToken}`;
  const tab = tabSelection?.scope === selectionScope ? tabSelection.tab : "overview";
  const pageKey = `${descriptorScope}\0${tab}`;
  const eventPageOrder =
    tab === "output" ? DescriptorPageOrder.NEWEST_FIRST : DescriptorPageOrder.FORWARD;
  const activeEventPage = eventPageScope === pageKey ? eventPage : EMPTY_EVENT_PAGE;
  const workflowDeclaration = run
    ? undefined
    : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId ?? ""]);
  const runFieldSchemas = run
    ? parseAgentFieldSchemas(run.topology?.agentFieldSchemasJson[nodeId ?? ""])
    : undefined;

  tabRef.current = tab;
  const abortDetailHydration = useCallback(() => {
    detailGeneration.current += 1;
    for (const controller of detailControllers.current) controller.abort();
    detailControllers.current.clear();
    detailLoading.current.clear();
  }, []);

  useEffect(
    () => () => {
      abortDetailHydration();
    },
    [abortDetailHydration],
  );

  function closeInspector() {
    abortDetailHydration();
    onClose();
  }

  function cacheKey(format: DetailFormat, token: string) {
    return `${format}\0${token}`;
  }

  function storeCachedDetail(key: string, value: unknown, reportedSize?: string) {
    const byteCost = measuredByteCost(value, reportedSize);
    if (byteCost > DETAIL_CACHE_MAX_BYTES) return false;
    detailCache.current.delete(key);
    detailCache.current.set(key, { value, byteCost });
    let cachedBytes = 0;
    for (const entry of detailCache.current.values()) cachedBytes += entry.byteCost;
    while (
      detailCache.current.size > DETAIL_CACHE_MAX_ENTRIES ||
      cachedBytes > DETAIL_CACHE_MAX_BYTES
    ) {
      const oldest = detailCache.current.entries().next().value as
        | [string, DetailCacheEntry]
        | undefined;
      if (!oldest) break;
      detailCache.current.delete(oldest[0]);
      cachedBytes -= oldest[1].byteCost;
    }
    setCacheVersion((current) => current + 1);
    return true;
  }

  function loadMoreEvents() {
    if (!activeEventPage.nextPageToken || !nodeId || !runId || pageRequestInFlight.current) return;
    pageController.current?.abort();
    const generation = ++pageGeneration.current;
    const controller = new AbortController();
    pageController.current = controller;
    pageRequestInFlight.current = true;
    setPageError(undefined);
    setPageLoading(true);
    void api
      .listAgentEventPage(
        {
          pageToken: activeEventPage.nextPageToken,
          afterEventSequence:
            eventPageOrder === DescriptorPageOrder.FORWARD ? activeEventPage.nextCursor : "0",
          beforeEventSequence:
            eventPageOrder === DescriptorPageOrder.NEWEST_FIRST ? activeEventPage.nextCursor : "0",
          pageSize: DESCRIPTOR_PAGE_SIZE,
          order: eventPageOrder,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfSequence: asOfSequence,
          expectedRunId: runId,
          expectedNodeId: nodeId,
        },
        controller.signal,
      )
      .then((page) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setEventPage((current) =>
          mergeDescriptorPage(
            current,
            page,
            (event) => event.eventSequence,
            eventPageOrder === DescriptorPageOrder.FORWARD ? "newer" : "older",
            valueEvent ? [valueEvent.eventSequence] : [],
          ),
        );
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError({
          key: pageKey,
          value: error instanceof Error ? error.message : "Events unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        pageRequestInFlight.current = false;
        setPageLoading(false);
      });
  }


  useEffect(() => {
    setTabSelection({ scope: selectionScope, tab: "overview" });
  }, [selectionScope]);

  useEffect(() => {
    pageController.current?.abort();
    pageRequestInFlight.current = false;
    abortDetailHydration();
    pageGeneration.current += 1;
    setEventPage(EMPTY_EVENT_PAGE);
    setEventPageScope(undefined);
    setPageError(undefined);
    setPageLoading(false);
    setFollowing(true);
    setDetailErrors({});
    setInputOutputState(undefined);
    detailCache.current.clear();
    detailLoading.current.clear();
  }, [abortDetailHydration, api, descriptorScope]);

  useEffect(() => {
    abortDetailHydration();
    setDetailLoadingVersion((current) => current + 1);
  }, [abortDetailHydration, descriptorScope, tab]);

  useEffect(() => {
    pageController.current?.abort();
    pageRequestInFlight.current = false;
    const generation = ++pageGeneration.current;
    setEventPage(EMPTY_EVENT_PAGE);
    setEventPageScope(undefined);
    setPageError(undefined);
    setPageLoading(false);
    if (!hasRunNode || !nodeId || !runId || tab === "overview") return;

    const controller = new AbortController();
    pageController.current = controller;
    if (!eventPageToken) {
      setEventPageScope(pageKey);
      return () => controller.abort();
    }

    pageRequestInFlight.current = true;
    setPageLoading(true);
    void api
      .listAgentEventPage(
        {
          pageToken: eventPageToken,
          afterEventSequence: "0",
          beforeEventSequence: "0",
          pageSize: DESCRIPTOR_PAGE_SIZE,
          order: eventPageOrder,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfSequence: asOfSequence,
          expectedRunId: runId,
          expectedNodeId: nodeId,
        },
        controller.signal,
      )
      .then((page) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setEventPage(page);
        setEventPageScope(pageKey);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError({
          key: pageKey,
          value: error instanceof Error ? error.message : "Events unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        pageRequestInFlight.current = false;
        setPageLoading(false);
      });
    return () => {
      controller.abort();
      if (pageController.current === controller) pageRequestInFlight.current = false;
    };
  }, [
    api,
    asOfSequence,
    descriptorScope,
    eventPageOrder,
    eventPageToken,
    hasRunNode,
    nodeId,
    operatorInstanceId,
    pageKey,
    runId,
    tab,
  ]);

  const combinedEvents = useMemo(() => {
    const bySequence = new Map<string, AgentEventDescriptorMsg>();
    const liveSequences = new Set<string>();
    for (const event of activeEventPage.records) bySequence.set(event.eventSequence, event);
    for (const event of liveEvents) {
      bySequence.set(event.eventSequence, event);
      liveSequences.add(event.eventSequence);
    }
    return boundDescriptors(
      bySequence,
      (event) => event.eventSequence,
      eventPageOrder === DescriptorPageOrder.FORWARD ? "newer" : "older",
      liveSequences,
    );
  }, [activeEventPage.records, eventPageOrder, liveEvents]);

  const turns = useMemo(
    () => combinedEvents.filter((event) => event.eventKind === "iteration.recorded"),
    [combinedEvents],
  );


  const valueEvent = useMemo(() => {
    if (tab !== "inputs" && tab !== "output") return undefined;
    const kind = tab === "inputs" ? "run.started" : "run.succeeded";
    return [...combinedEvents].reverse().find((event) => event.eventKind === kind);
  }, [combinedEvents, tab]);
  const valueDetailKey = valueEvent
    ? `${descriptorScope}\0${tab}\0${valueEvent.bodyToken}`
    : undefined;

  useEffect(() => {
    if ((tab !== "inputs" && tab !== "output") || !valueEvent || !valueDetailKey) {
      setInputOutputState(undefined);
      return;
    }
    const key = cacheKey("json", valueEvent.bodyToken);
    const cached = detailCache.current.get(key);
    if (cached) {
      detailCache.current.delete(key);
      detailCache.current.set(key, cached);
      setInputOutputState({ key: valueDetailKey, status: "ready" });
      return;
    }

    const generation = detailGeneration.current;
    const controller = new AbortController();
    detailControllers.current.add(controller);
    setInputOutputState({ key: valueDetailKey, status: "loading" });
    void api
      .readJsonDetail(valueEvent.bodyToken, controller.signal)
      .then((body) => {
        if (
          controller.signal.aborted ||
          detailGeneration.current !== generation ||
          tabRef.current !== tab
        ) return;
        if (!storeCachedDetail(key, body, valueEvent.sizeBytes)) {
          setInputOutputState({
            key: valueDetailKey,
            status: "error",
            error: "Retained value exceeds the browser detail limit.",
          });
          return;
        }
        setInputOutputState({ key: valueDetailKey, status: "ready" });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        setInputOutputState({
          key: valueDetailKey,
          status: "error",
          error: error instanceof Error ? error.message : "Detail unavailable",
        });
      })
      .finally(() => detailControllers.current.delete(controller));
    return () => {
      controller.abort();
      detailControllers.current.delete(controller);
    };
  }, [api, cacheVersion, descriptorScope, tab, valueDetailKey, valueEvent]);

  function hydrateTraceTurn(event: AgentEventDescriptorMsg) {
    if (tabRef.current !== "trace") return;
    const key = cacheKey("json", event.bodyToken);
    if (detailCache.current.has(key) || detailLoading.current.has(key)) return;
    const generation = detailGeneration.current;
    const controller = new AbortController();
    detailControllers.current.add(controller);
    detailLoading.current.add(key);
    setDetailErrors((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    setDetailLoadingVersion((current) => current + 1);
    void api
      .readJsonDetail(event.bodyToken, controller.signal)
      .then((body) => {
        if (
          controller.signal.aborted ||
          detailGeneration.current !== generation ||
          tabRef.current !== "trace"
        ) return;
        if (!storeCachedDetail(key, body, event.sizeBytes)) {
          setDetailErrors((current) => ({
            ...current,
            [key]: "Turn detail exceeds the browser detail limit.",
          }));
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        setDetailErrors((current) => ({
          ...current,
          [key]: error instanceof Error ? error.message : "Turn detail unavailable",
        }));
      })
      .finally(() => {
        detailControllers.current.delete(controller);
        detailLoading.current.delete(key);
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        setDetailLoadingVersion((current) => current + 1);
      });
  }

  const traceProjection = useMemo(() => {
    const descriptors = new WeakMap<object, AgentEventDescriptorMsg>();
    const values = turns.map((event) => {
      const key = cacheKey("json", event.bodyToken);
      const cached = detailCache.current.get(key)?.value;
      const error = detailErrors[key];
      const loading = detailLoading.current.has(key);
      const value = {
        ...eventSummary(event),
        ...(isUnknownRecord(cached)
          ? cached
          : cached !== undefined
            ? { detail: cached }
            : {
                detail: error
                  ? { kind: "unavailable", reason: error }
                  : loading
                    ? "Loading retained turn…"
                    : "Expand this turn to load its retained detail.",
              }),
      };
      descriptors.set(value, event);
      return value;
    });
    return { descriptors, values };
  }, [cacheVersion, detailErrors, detailLoadingVersion, turns]);

  const traceRoot = useMemo(() => {
    const header = node?.trace?.header;
    return {
      status: node?.trace?.status,
      complete: node?.trace?.complete,
      event_count: node?.trace?.eventCount,
      size_bytes: node?.trace?.sizeBytes,
      model: header?.model,
      sub_model: header?.subModel,
      iterations: header?.iterations,
      max_iterations: header?.maxIterations,
      duration_ms: header?.durationMs,
      usage: parseRetainedJson(header?.usageJson),
      telemetry: parseRetainedJson(header?.telemetryJson),
      lifecycle: combinedEvents
        .filter((event) => event.eventKind !== "iteration.recorded")
        .map(eventSummary),
      turns: traceProjection.values,
    };
  }, [combinedEvents, node?.trace, traceProjection.values]);

  useEffect(() => {
    if (tab !== "trace" || !following || !turns.length || !traceScrollElement.current) return;
    traceScrollElement.current.scrollTop = traceScrollElement.current.scrollHeight;
  }, [following, tab, turns.length]);


  if (!run && workflow && nodeId) {
    return (
      <aside className="inspector inspector-declaration" aria-label="Node declaration">
        <header>
          <div>
            <span className="eyebrow">Declaration</span>
            <h2>{workflow.displayNames[nodeId] || nodeId}</h2>
          </div>
          <button type="button" className="icon-button" onClick={closeInspector} aria-label="Close">
            ×
          </button>
        </header>
        {workflowDeclaration ? (
          <div className="inspector-body inspector-body-full declaration">
            <section>
              <h3>Instructions</h3>
              <Markdown className="instructions">
                {workflowDeclaration.instructions || "No instructions"}
              </Markdown>
            </section>
            <section className="signature-columns">
              <div>
                <h3>Inputs</h3>
                {workflowDeclaration.inputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    {field.description && <p>{field.description}</p>}
                  </div>
                ))}
              </div>
              <div>
                <h3>Outputs</h3>
                {workflowDeclaration.outputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    {field.description && <p>{field.description}</p>}
                  </div>
                ))}
              </div>
            </section>
            {workflowDeclaration.runtime !== undefined && (
              <section>
                <h3>Runtime</h3>
                <ValueView value={workflowDeclaration.runtime} />
              </section>
            )}
            {workflowDeclaration.model !== undefined && (
              <section>
                <h3>Models</h3>
                <ValueView value={workflowDeclaration.model} />
              </section>
            )}
            {(workflowDeclaration.skills.length > 0 || workflowDeclaration.tools.length > 0) && (
              <section className="inspector-declaration-resources">
                <h3>Skills &amp; tools</h3>
                {workflowDeclaration.skills.map((skill) => (
                  <article className="inspector-declaration-resource" key={`skill-${skill.name}`}>
                    <strong>{skill.name}</strong>
                    <span>Skill</span>
                    <Markdown>{skill.instructions}</Markdown>
                  </article>
                ))}
                {workflowDeclaration.tools.map((tool) => (
                  <article className="inspector-declaration-resource" key={`tool-${tool.name}`}>
                    <strong>{tool.name}</strong>
                    <span>Tool</span>
                    <Markdown>{tool.description}</Markdown>
                  </article>
                ))}
              </section>
            )}
          </div>
        ) : (
          <p className="empty-copy">This node has no agent declaration metadata.</p>
        )}
      </aside>
    );
  }

  if (!run || !node) return null;
  const declaredFields =
    tab === "inputs"
      ? runFieldSchemas?.inputs
      : tab === "output"
        ? runFieldSchemas?.outputs
        : undefined;
  const valueCacheEntry = valueEvent
    ? detailCache.current.get(cacheKey("json", valueEvent.bodyToken))
    : undefined;
  const selectedPayload = eventPayload(valueCacheEntry?.value);
  const valueKey = tab === "inputs" ? "inputs" : "outputs";
  const activePageError = pageError?.key === pageKey ? pageError.value : undefined;
  const activeInputOutputState =
    valueDetailKey !== undefined && inputOutputState?.key === valueDetailKey
      ? inputOutputState
      : undefined;
  const inputOutputLoading =
    !activePageError &&
    (eventPageScope !== pageKey ||
      (valueDetailKey !== undefined &&
        valueCacheEntry === undefined &&
        activeInputOutputState?.status !== "error"));
  const inputOutputError =
    activeInputOutputState?.status === "error" ? activeInputOutputState.error : undefined;

  return (
    <aside className="inspector inspector-run" aria-label="Run inspector">
      <header>
        <div>
          <span className="eyebrow">Execution detail</span>
          <h2>{node.name}</h2>
          <span className={`status-pill status-${node.status}`}>{node.status}</span>
        </div>
        <button type="button" className="icon-button" onClick={closeInspector} aria-label="Close">
          ×
        </button>
      </header>
      <nav className="inspector-tabs" aria-label="Run detail views">
        {(["overview", "inputs", "output", "trace"] as RunTab[]).map((item) => (
          <button
            type="button"
            key={item}
            className={tab === item ? "active" : ""}
            aria-current={tab === item ? "page" : undefined}
            onClick={() => setTabSelection({ scope: selectionScope, tab: item })}
          >
            {item}
          </button>
        ))}
      </nav>
      <div className="inspector-body inspector-body-full">
        {tab === "overview" && (
          <section className="inspector-panel inspector-overview">
            <div className="metric-grid">
              <div><small>Status</small><strong>{node.status}</strong></div>
              <div><small>Started</small><strong>{node.startedAt ? "yes" : "—"}</strong></div>
              <div>
                <small>Duration</small>
                <strong>
                  {node.startedAt && node.endedAt
                    ? `${Math.max(0, node.endedAt - node.startedAt).toFixed(2)}s`
                    : "—"}
                </strong>
              </div>
            </div>
            {node.error && <p className="node-failure">{node.error}</p>}
            {node.trace && (
              <section>
                <h3>Trace summary</h3>
                <ValueView
                  value={{
                    status: node.trace.status,
                    events: node.trace.eventCount,
                    size_bytes: node.trace.sizeBytes,
                    complete: node.trace.complete,
                    model: node.trace.header?.model,
                    iterations: node.trace.header
                      ? `${node.trace.header.iterations}/${node.trace.header.maxIterations}`
                      : undefined,
                    duration_ms: node.trace.header?.durationMs,
                    usage: parseRetainedJson(node.trace.header?.usageJson),
                    telemetry: parseRetainedJson(node.trace.header?.telemetryJson),
                  }}
                />
              </section>
            )}
          </section>
        )}

        {(tab === "inputs" || tab === "output") && (
          <section className="inspector-panel inspector-value-panel">
            <h3>{tab === "inputs" ? "Invocation inputs" : "Terminal output"}</h3>
            {declaredFields?.length ? (
              <div className="declared-fields">
                <small>Declared fields</small>
                {declaredFields.map((field) => (
                  <span key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                  </span>
                ))}
              </div>
            ) : null}
            {activePageError && <p className="inspector-error" role="alert">{activePageError}</p>}
            {inputOutputError && <p className="inspector-error" role="alert">{inputOutputError}</p>}
            {inputOutputLoading ? (
              <p className="inspector-loading" role="status">
                Loading retained {tab === "inputs" ? "inputs" : "output"}…
              </p>
            ) : selectedPayload && valueKey in selectedPayload ? (
              <ValueView value={selectedPayload[valueKey]} />
            ) : (
              <p className="empty-copy">
                No retained {tab} {tab === "output" ? "is" : "are"} available.
              </p>
            )}
            {activeEventPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action"
                disabled={pageLoading}
                aria-busy={pageLoading}
                onClick={loadMoreEvents}
              >
                {pageLoading ? "Loading events…" : "Load more events"}
              </button>
            )}
          </section>
        )}

        {tab === "trace" && (
          <section className="inspector-panel inspector-trace-panel">
            <div className="trace-toolbar">
              <div>
                <h3>RunTrace</h3>
                <span>{turns.length} retained {turns.length === 1 ? "turn" : "turns"}</span>
              </div>
              <button
                type="button"
                className={following ? "toggle active" : "toggle"}
                onClick={() => setFollowing((value) => !value)}
              >
                {following ? "Following live" : "Follow latest"}
              </button>
            </div>
            {activePageError && <p className="inspector-error" role="alert">{activePageError}</p>}
            {pageLoading && !combinedEvents.length && (
              <p className="inspector-loading" role="status">Loading retained trace…</p>
            )}
            <div
              className="inspector-trace-explorer"
              ref={traceScrollElement}
              onScroll={(event) => {
                const element = event.currentTarget;
                const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
                if (distanceFromBottom > SCROLL_LOAD_THRESHOLD_PX) setFollowing(false);
                if (distanceFromBottom <= SCROLL_LOAD_THRESHOLD_PX && activeEventPage.nextPageToken) {
                  loadMoreEvents();
                }
              }}
            >
              <ValueView
                value={traceRoot}
                onExpand={(value) => {
                  if (value === traceProjection.values) {
                    if (activeEventPage.nextPageToken) loadMoreEvents();
                    return;
                  }
                  if (typeof value !== "object" || value === null) return;
                  const descriptor = traceProjection.descriptors.get(value);
                  if (descriptor) hydrateTraceTurn(descriptor);
                }}
              />
              {pageLoading && combinedEvents.length > 0 && (
                <p className="inspector-loading" role="status">Loading more retained trace…</p>
              )}
              {!pageLoading && !activeEventPage.nextPageToken && combinedEvents.length > 0 && (
                <p className="inspector-end-state">End of retained trace</p>
              )}
            </div>
            {activeEventPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action"
                disabled={pageLoading}
                aria-busy={pageLoading}
                onClick={loadMoreEvents}
              >
                {pageLoading ? "Loading events…" : "Load more trace"}
              </button>
            )}
          </section>
        )}

      </div>
    </aside>
  );
}
