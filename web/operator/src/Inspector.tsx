import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";

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
} from "./model";
import { isUnknownRecord } from "./guards";
import { Markdown } from "./Markdown";
import { PythonSource } from "./PythonSource";
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

type NodeSourceState =
  | { key: string; status: "loading" }
  | { key: string; status: "ready"; sourceCode: string | undefined }
  | { key: string; status: "error"; error: string };

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

  const [nodeSourceState, setNodeSourceState] = useState<NodeSourceState>();
  const detailCache = useRef(new Map<string, DetailCacheEntry>());
  const nodeSourceCache = useRef(new Map<string, string | undefined>());
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
  const asOfEventUlid = run?.asOfEventUlid ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const hasRunNode = Boolean(run && node);
  const selectionScope = `${operatorInstanceId}\0${runId ?? ""}\0${nodeId ?? ""}`;
  const descriptorScope = `${selectionScope}\0${asOfEventUlid}\0${eventPageToken}`;
  const tab = tabSelection?.scope === selectionScope ? tabSelection.tab : "overview";
  const pageKey = `${descriptorScope}\0${tab}`;
  const eventPageOrder =
    tab === "output" ? DescriptorPageOrder.NEWEST_FIRST : DescriptorPageOrder.FORWARD;
  const activeEventPage = eventPageScope === pageKey ? eventPage : EMPTY_EVENT_PAGE;
  const isWorkflowAgentNode =
    !run &&
    workflow !== undefined &&
    nodeId !== undefined &&
    workflow.agentNodeIds.includes(nodeId);
  const workflowDeclaration =
    run || !isWorkflowAgentNode
      ? undefined
      : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId ?? ""]);
  const runFieldSchemas = run
    ? parseAgentFieldSchemas(run.topology?.agentFieldSchemasJson[nodeId ?? ""])
    : undefined;
  const sourceWorkflowSelector =
    !run && workflow !== undefined && nodeId !== undefined && !isWorkflowAgentNode
      ? workflow.name
      : undefined;
  const nodeSourceScope =
    sourceWorkflowSelector !== undefined && nodeId !== undefined
      ? `${sourceWorkflowSelector}\0${nodeId}`
      : undefined;
  const activeNodeSource =
    nodeSourceScope !== undefined && nodeSourceState?.key === nodeSourceScope
      ? nodeSourceState
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

  useEffect(() => {
    if (
      nodeSourceScope === undefined ||
      sourceWorkflowSelector === undefined ||
      nodeId === undefined
    ) {
      setNodeSourceState(undefined);
      return;
    }
    if (nodeSourceCache.current.has(nodeSourceScope)) {
      setNodeSourceState({
        key: nodeSourceScope,
        status: "ready",
        sourceCode: nodeSourceCache.current.get(nodeSourceScope),
      });
      return;
    }
    const controller = new AbortController();
    setNodeSourceState({ key: nodeSourceScope, status: "loading" });
    void api
      .getWorkflowNodeSource(sourceWorkflowSelector, nodeId, controller.signal)
      .then((sourceCode) => {
        if (controller.signal.aborted) return;
        nodeSourceCache.current.set(nodeSourceScope, sourceCode);
        setNodeSourceState({ key: nodeSourceScope, status: "ready", sourceCode });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setNodeSourceState({
          key: nodeSourceScope,
          status: "error",
          error: error instanceof Error ? error.message : "Source code unavailable",
        });
      });
    return () => controller.abort();
  }, [api, nodeId, nodeSourceScope, sourceWorkflowSelector]);

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
        [string, DetailCacheEntry] | undefined;
      if (!oldest) break;
      detailCache.current.delete(oldest[0]);
      cachedBytes -= oldest[1].byteCost;
    }
    setCacheVersion((current) => current + 1);
    return true;
  }

  function loadMoreEvents() {
    if (!activeEventPage.nextPageToken || !nodeId || !runId || pageRequestInFlight.current)
      return;
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
            eventPageOrder === DescriptorPageOrder.NEWEST_FIRST
              ? activeEventPage.nextCursor
              : "0",
          pageSize: DESCRIPTOR_PAGE_SIZE,
          order: eventPageOrder,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfEventUlid: asOfEventUlid,
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
          expectedAsOfEventUlid: asOfEventUlid,
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
    asOfEventUlid,
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
        )
          return;
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
        )
          return;
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
      <aside
        className="inspector inspector-declaration fixed top-[58px] right-0 bottom-0 z-30 grid h-auto w-[min(var(--workspace-inspector-width),100vw)] min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden border-l border-line bg-panel shadow-[-20px_0_50px_rgba(20,31,26,.14)] min-[1001px]:static min-[1001px]:z-auto min-[1001px]:h-full min-[1001px]:w-auto min-[1001px]:shadow-none max-[700px]:w-screen"
        aria-label={isWorkflowAgentNode ? "Node declaration" : "Node code"}
      >
        <header className="flex items-start justify-between border-b border-line px-5 pt-[19px] pb-3.5">
          <div>
            <span className="eyebrow block font-mono text-[9px] tracking-[.16em] text-acid uppercase">
              {isWorkflowAgentNode ? "Declaration" : "Code"}
            </span>
            <h2 className="mt-1 mb-[5px] text-lg">{workflow.displayNames[nodeId] || nodeId}</h2>
          </div>
          <button
            type="button"
            className="icon-button grid size-[30px] cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary hover:border-secondary hover:bg-panel hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
            onClick={closeInspector}
            aria-label="Close"
          >
            <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
          </button>
        </header>
        {isWorkflowAgentNode ? (
          workflowDeclaration ? (
            <div className="inspector-body inspector-body-full declaration h-full min-h-0 min-w-0 overflow-auto px-5 pt-[18px] pb-[30px] [&>section]:mb-[23px] [&_h3]:text-[10px] [&_h3]:tracking-[.08em] [&_h3]:text-secondary [&_h3]:uppercase">
              <section>
                <h3>Instructions</h3>
                <Markdown className="instructions text-xs leading-[1.65] whitespace-normal text-secondary [&>:first-child]:mt-0 [&>:last-child]:mb-0">
                  {workflowDeclaration.instructions || "No instructions"}
                </Markdown>
              </section>
              <section className="signature-columns grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
                <div>
                  <h3>Inputs</h3>
                  {workflowDeclaration.inputs.map((field) => (
                    <div
                      className="field-detail border-t border-line py-2 [&_strong]:block [&_strong]:text-[10px] [&_code]:mt-0.5 [&_code]:block [&_code]:text-[8px] [&_code]:text-muted [&_code]:[overflow-wrap:anywhere] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[9px] [&_p]:text-muted [&_p]:[overflow-wrap:anywhere]"
                      key={field.name}
                    >
                      <strong>{field.name}</strong>
                      <code>{field.type}</code>
                      {field.description && <p>{field.description}</p>}
                    </div>
                  ))}
                </div>
                <div>
                  <h3>Outputs</h3>
                  {workflowDeclaration.outputs.map((field) => (
                    <div
                      className="field-detail border-t border-line py-2 [&_strong]:block [&_strong]:text-[10px] [&_code]:mt-0.5 [&_code]:block [&_code]:text-[8px] [&_code]:text-muted [&_code]:[overflow-wrap:anywhere] [&_p]:mt-1 [&_p]:mb-0 [&_p]:text-[9px] [&_p]:text-muted [&_p]:[overflow-wrap:anywhere]"
                      key={field.name}
                    >
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
              {(workflowDeclaration.skills.length > 0 ||
                workflowDeclaration.tools.length > 0) && (
                <section className="inspector-declaration-resources grid gap-3">
                  <h3>Skills &amp; tools</h3>
                  {workflowDeclaration.skills.map((skill) => (
                    <article
                      className="inspector-declaration-resource min-w-0 border-t border-line pt-2 text-[10px] leading-[1.55] text-secondary [&>strong]:inline [&>span]:ml-1.5 [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-muted [&>span]:uppercase [&>div]:mt-1.5 [&>div]:[overflow-wrap:anywhere] [&>div>:last-child]:mb-0"
                      key={`skill-${skill.name}`}
                    >
                      <strong>{skill.name}</strong>
                      <span>Skill</span>
                      <Markdown>{skill.instructions}</Markdown>
                    </article>
                  ))}
                  {workflowDeclaration.tools.map((tool) => (
                    <article
                      className="inspector-declaration-resource min-w-0 border-t border-line pt-2 text-[10px] leading-[1.55] text-secondary [&>strong]:inline [&>span]:ml-1.5 [&>span]:font-mono [&>span]:text-[8px] [&>span]:text-muted [&>span]:uppercase [&>div]:mt-1.5 [&>div]:[overflow-wrap:anywhere] [&>div>:last-child]:mb-0"
                      key={`tool-${tool.name}`}
                    >
                      <strong>{tool.name}</strong>
                      <span>Tool</span>
                      <Markdown>{tool.description}</Markdown>
                    </article>
                  ))}
                </section>
              )}
            </div>
          ) : (
            <p className="empty-copy text-[11px] text-muted">
              This node has no agent declaration metadata.
            </p>
          )
        ) : activeNodeSource?.status === "ready" ? (
          activeNodeSource.sourceCode !== undefined ? (
            <div className="inspector-body inspector-body-full h-full min-h-0 min-w-0 overflow-hidden">
              <PythonSource source={activeNodeSource.sourceCode} />
            </div>
          ) : (
            <p className="empty-copy text-[11px] text-muted">
              Source code is unavailable for this node.
            </p>
          )
        ) : activeNodeSource?.status === "error" ? (
          <p className="empty-copy text-[11px] text-muted" role="alert">
            Source code is unavailable: {activeNodeSource.error}
          </p>
        ) : (
          <p className="empty-copy text-[11px] text-muted" role="status">
            Loading source code…
          </p>
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
    <aside
      className="inspector inspector-run fixed top-[58px] right-0 bottom-0 z-30 grid h-auto w-[min(var(--workspace-inspector-width),100vw)] min-h-0 min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] overflow-hidden border-l border-line bg-panel shadow-[-20px_0_50px_rgba(20,31,26,.14)] min-[1001px]:static min-[1001px]:z-auto min-[1001px]:h-full min-[1001px]:w-auto min-[1001px]:shadow-none max-[700px]:w-screen"
      aria-label="Run inspector"
    >
      <header className="flex items-start justify-between border-b border-line px-5 pt-[19px] pb-3.5">
        <div>
          <span className="eyebrow block font-mono text-[9px] tracking-[.16em] text-acid uppercase">
            Execution detail
          </span>
          <h2 className="mt-1 mb-[5px] text-lg">{node.name}</h2>
          <span
            className={`status-pill inline-flex rounded-full border bg-panel px-[7px] py-[3px] font-mono text-[8px] uppercase ${node.status === "failed" ? "status-failed border-danger text-danger" : node.status === "success" ? "status-success border-mint text-mint" : "border-line text-muted"}`}
          >
            {node.status}
          </span>
        </div>
        <button
          type="button"
          className="icon-button grid size-[30px] cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary hover:border-secondary hover:bg-panel hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
          onClick={closeInspector}
          aria-label="Close"
        >
          <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
        </button>
      </header>
      <nav
        className="inspector-tabs flex overflow-x-auto border-b border-line px-2.5"
        aria-label="Run detail views"
      >
        {(["overview", "inputs", "output", "trace"] as RunTab[]).map((item) => (
          <button
            type="button"
            key={item}
            className={`flex-[1_0_auto] cursor-pointer border-0 border-b-2 bg-transparent px-[9px] pt-[11px] pb-[9px] font-mono text-[8px] uppercase focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid ${tab === item ? "active border-acid text-acid" : "border-transparent text-muted"}`}
            aria-current={tab === item ? "page" : undefined}
            onClick={() => setTabSelection({ scope: selectionScope, tab: item })}
          >
            {item}
          </button>
        ))}
      </nav>
      <div className="inspector-body inspector-body-full h-full min-h-0 min-w-0 overflow-auto px-5 pt-[18px] pb-[30px] [&>section]:mb-[23px] [&_h3]:text-[10px] [&_h3]:tracking-[.08em] [&_h3]:text-secondary [&_h3]:uppercase">
        {tab === "overview" && (
          <section className="inspector-panel inspector-overview min-h-full min-w-0">
            <div className="metric-grid grid grid-cols-2 gap-2 [&>div]:rounded-[7px] [&>div]:border [&>div]:border-line [&>div]:bg-panel [&>div]:p-2.5 [&_small]:block [&_small]:text-[7px] [&_small]:text-muted [&_small]:uppercase [&_strong]:mt-[5px] [&_strong]:block [&_strong]:text-[11px]">
              <div>
                <small>Status</small>
                <strong>{node.status}</strong>
              </div>
              <div>
                <small>Started</small>
                <strong>{node.startedAt ? "yes" : "—"}</strong>
              </div>
              <div>
                <small>Duration</small>
                <strong>
                  {node.startedAt && node.endedAt
                    ? `${Math.max(0, node.endedAt - node.startedAt).toFixed(2)}s`
                    : "—"}
                </strong>
              </div>
            </div>
            {node.error && (
              <p className="node-failure rounded-[7px] border border-danger p-2.5 text-[10px] text-danger [overflow-wrap:anywhere]">
                {node.error}
              </p>
            )}
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
          <section className="inspector-panel inspector-value-panel min-h-full min-w-0">
            <h3>{tab === "inputs" ? "Invocation inputs" : "Terminal output"}</h3>
            {declaredFields?.length ? (
              <div className="declared-fields mb-2.5 flex flex-wrap gap-[5px] [&>small]:w-full [&>small]:text-[8px] [&>small]:text-muted [&>small]:uppercase [&>span]:inline-flex [&>span]:gap-[5px] [&>span]:rounded-[5px] [&>span]:border [&>span]:border-line [&>span]:bg-panel [&>span]:px-1.5 [&>span]:py-1 [&>span]:text-[9px] [&_code]:text-secondary">
                <small>Declared fields</small>
                {declaredFields.map((field) => (
                  <span key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                  </span>
                ))}
              </div>
            ) : null}
            {activePageError && (
              <p
                className="inspector-error rounded-[7px] border border-danger p-2.5 text-[10px] text-danger [overflow-wrap:anywhere]"
                role="alert"
              >
                {activePageError}
              </p>
            )}
            {inputOutputError && (
              <p
                className="inspector-error rounded-[7px] border border-danger p-2.5 text-[10px] text-danger [overflow-wrap:anywhere]"
                role="alert"
              >
                {inputOutputError}
              </p>
            )}
            {inputOutputLoading ? (
              <p className="inspector-loading text-[11px] text-muted italic" role="status">
                Loading retained {tab === "inputs" ? "inputs" : "output"}…
              </p>
            ) : selectedPayload && valueKey in selectedPayload ? (
              <ValueView value={selectedPayload[valueKey]} />
            ) : (
              <p className="empty-copy text-[11px] text-muted">
                No retained {tab} {tab === "output" ? "is" : "are"} available.
              </p>
            )}
            {activeEventPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action cursor-pointer rounded-md border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
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
          <section className="inspector-panel inspector-trace-panel mb-0! flex h-full min-h-full min-w-0 flex-col gap-3">
            <div className="trace-toolbar flex items-center justify-between gap-2 [&_h3]:mt-0 [&_h3]:mb-[3px] [&_span]:font-mono [&_span]:text-[8px] [&_span]:text-muted">
              <div>
                <h3>RunTrace</h3>
                <span>
                  {turns.length} retained {turns.length === 1 ? "turn" : "turns"}
                </span>
              </div>
              <button
                type="button"
                className={`toggle flex-none cursor-pointer rounded-full border bg-panel px-2 py-[5px] font-mono text-[8px] ${following ? "active border-acid text-acid" : "border-line text-secondary"}`}
                onClick={() => setFollowing((value) => !value)}
              >
                {following ? "Following live" : "Follow latest"}
              </button>
            </div>
            {activePageError && (
              <p
                className="inspector-error rounded-[7px] border border-danger p-2.5 text-[10px] text-danger [overflow-wrap:anywhere]"
                role="alert"
              >
                {activePageError}
              </p>
            )}
            {pageLoading && !combinedEvents.length && (
              <p className="inspector-loading text-[11px] text-muted italic" role="status">
                Loading retained trace…
              </p>
            )}
            <div
              className="inspector-trace-explorer min-h-48 min-w-0 flex-[1_1_auto] overflow-auto rounded-[7px] border border-line bg-panel p-2"
              ref={traceScrollElement}
              onScroll={(event) => {
                const element = event.currentTarget;
                const distanceFromBottom =
                  element.scrollHeight - element.scrollTop - element.clientHeight;
                if (distanceFromBottom > SCROLL_LOAD_THRESHOLD_PX) setFollowing(false);
                if (
                  distanceFromBottom <= SCROLL_LOAD_THRESHOLD_PX &&
                  activeEventPage.nextPageToken
                ) {
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
                <p className="inspector-loading text-[11px] text-muted italic" role="status">
                  Loading more retained trace…
                </p>
              )}
              {!pageLoading && !activeEventPage.nextPageToken && combinedEvents.length > 0 && (
                <p className="inspector-end-state mt-2 text-center font-mono text-[8px] text-muted uppercase">
                  End of retained trace
                </p>
              )}
            </div>
            {activeEventPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action cursor-pointer rounded-md border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
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
