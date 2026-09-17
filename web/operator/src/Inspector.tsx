import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";

import { AgentTraceExplorer, type AgentTraceTurn } from "./AgentTraceExplorer";
import {
  type AgentTraceStep,
  type AgentTraceStepSummary,
  type TraceLmUsage,
  parseAgentTraceUsage,
  parseAgentTraceStepEvent,
  summarizeAgentTraceStep,
} from "./agentTrace";

import type { OperatorApi } from "./api";
import {
  boundDescriptors,
  DESCRIPTOR_PAGE_SIZE,
  DESCRIPTOR_WINDOW_SIZE,
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
import { Markdown } from "./Markdown";
import { PythonSource } from "./PythonSource";
import { InspectorFields, InspectorResources } from "./InspectorDefinition";
import { RetainedAgentValue } from "./RetainedAgentValue";
import { isUnknownRecord } from "./guards";

interface InspectorProps {
  api: OperatorApi;
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  nodeId?: string;
  liveEvents?: AgentEventDescriptorMsg[];
  embedded?: boolean;
  definitionLabel?: string;
  onClose: () => void;
}

type AgentTab = "trace" | "io";
type DetailFormat = "json";

interface ScopedResult<T> {
  key: string;
  value: T;
}

type CachedDetail = { body: unknown; step: AgentTraceStep };

interface DetailCacheEntry {
  detail: CachedDetail;
  byteCost: number;
}

type NodeSourceState = {
  api: OperatorApi;
  workflow: FlowInfoMsg;
  key: string;
} & (
  | { status: "loading" }
  | { status: "ready"; sourceCode: string | undefined }
  | { status: "error"; error: string }
);

const DETAIL_CACHE_MAX_ENTRIES = 8;
const TOKEN_NUMBER_FORMAT = new Intl.NumberFormat();

const EMPTY_EVENTS: AgentEventDescriptorMsg[] = [];
const EMPTY_EVENT_PAGE: DescriptorPageState<AgentEventDescriptorMsg> = {
  records: EMPTY_EVENTS,
  nextPageToken: "",
  nextCursor: "0",
};

function formatSeconds(seconds: number) {
  return `${Math.max(0, seconds).toFixed(2)}s`;
}

function formatTraceDuration(durationMs: string) {
  const parsed = Number(durationMs);
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error("Trace duration must be a non-negative number");
  }
  return formatSeconds(parsed / 1000);
}

function declaredModelName(value: unknown): string | undefined {
  if (!isUnknownRecord(value)) return undefined;
  const identity = value.identity;
  if (typeof identity === "string") return identity;
  if (!isUnknownRecord(identity)) return undefined;
  if (typeof identity.name === "string") return identity.name;
  if (typeof identity.model === "string") return identity.model;
  return typeof identity.type === "string" ? identity.type : undefined;
}

export function Inspector({
  api,
  workflow,
  run,
  nodeId,
  liveEvents = EMPTY_EVENTS,
  embedded = false,
  definitionLabel = "Current definition",
  onClose,
}: InspectorProps) {
  const [selectedTab, setSelectedTab] = useState<AgentTab>("trace");
  const [eventPage, setEventPage] =
    useState<DescriptorPageState<AgentEventDescriptorMsg>>(EMPTY_EVENT_PAGE);
  const [eventPageScope, setEventPageScope] = useState<string>();
  const [pageError, setPageError] = useState<ScopedResult<string>>();
  const [pageLoading, setPageLoading] = useState(false);
  const [following, setFollowing] = useState(true);
  const [cacheVersion, setCacheVersion] = useState(0);
  const [detailErrors, setDetailErrors] = useState<
    Record<string, { error: string; retryable: boolean }>
  >({});
  const [detailLoadingVersion, setDetailLoadingVersion] = useState(0);

  const [nodeSourceState, setNodeSourceState] = useState<NodeSourceState>();
  const detailCache = useRef(new Map<string, DetailCacheEntry>());
  const openTurnDetails = useRef(new Set<string>());
  const traceSummaries = useRef(new Map<string, AgentTraceStepSummary>());
  const detailLoading = useRef(new Set<string>());
  const detailControllers = useRef(new Set<AbortController>());
  const pageController = useRef<AbortController | null>(null);
  const pageRequestInFlight = useRef(false);
  const pageGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const traceScrollElement = useRef<HTMLDivElement>(null);
  const tabRef = useRef<AgentTab>("trace");
  const tabsId = useId();

  const node: NodeSnapshotMsg | undefined = run?.nodes.find((item) => item.nodeId === nodeId);
  const runId = run?.summary?.runId;
  const operatorInstanceId = run?.operatorInstanceId ?? "";
  const asOfEventUlid = run?.asOfEventUlid ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const hasRunNode = Boolean(run && node);
  const selectionScope = `${operatorInstanceId}\0${runId ?? ""}\0${nodeId ?? ""}`;
  const descriptorScope = `${selectionScope}\0${asOfEventUlid}\0${eventPageToken}`;
  const tab = selectedTab;
  const pageKey = `${descriptorScope}\0${tab}`;
  const eventPageOrder = DescriptorPageOrder.FORWARD;
  const activeEventPage = eventPageScope === pageKey ? eventPage : EMPTY_EVENT_PAGE;
  const currentNodeExists = Boolean(workflow && nodeId && workflow.nodeIds.includes(nodeId));
  const isWorkflowAgentNode = Boolean(
    nodeId && currentNodeExists && workflow?.agentNodeIds.includes(nodeId),
  );
  const isAgentNode = run
    ? run.topology
      ? Object.hasOwn(run.topology.agentFieldSchemasJson, nodeId ?? "")
      : Boolean(node?.trace)
    : isWorkflowAgentNode;
  const workflowDeclaration = useMemo(
    () =>
      isWorkflowAgentNode
        ? parseAgentDeclaration(workflow?.agentMetadataJson[nodeId ?? ""])
        : undefined,
    [isWorkflowAgentNode, nodeId, workflow],
  );
  const historicalFieldSchemas = useMemo(
    () => parseAgentFieldSchemas(run?.topology?.agentFieldSchemasJson[nodeId ?? ""]),
    [nodeId, run],
  );
  const sourceWorkflowSelector =
    !run && currentNodeExists && !isAgentNode ? workflow?.name : undefined;
  const nodeSourceScope =
    sourceWorkflowSelector !== undefined && nodeId !== undefined
      ? `${sourceWorkflowSelector}\0${nodeId}`
      : undefined;
  const activeNodeSource =
    nodeSourceScope !== undefined &&
    nodeSourceState?.key === nodeSourceScope &&
    nodeSourceState.api === api &&
    nodeSourceState.workflow === workflow
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
      nodeId === undefined ||
      workflow === undefined
    ) {
      setNodeSourceState(undefined);
      return;
    }
    const controller = new AbortController();
    setNodeSourceState({ api, workflow, key: nodeSourceScope, status: "loading" });
    void api
      .getWorkflowNodeSource(sourceWorkflowSelector, nodeId, controller.signal)
      .then((sourceCode) => {
        if (controller.signal.aborted) return;
        setNodeSourceState({
          api,
          workflow,
          key: nodeSourceScope,
          status: "ready",
          sourceCode,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setNodeSourceState({
          api,
          workflow,
          key: nodeSourceScope,
          status: "error",
          error: error instanceof Error ? error.message : "Source code unavailable",
        });
      });
    return () => controller.abort();
  }, [api, nodeId, nodeSourceScope, sourceWorkflowSelector, workflow]);

  function closeInspector() {
    abortDetailHydration();
    onClose();
  }

  const panelLayout = embedded
    ? "relative h-full w-full"
    : "fixed right-0 bottom-0 top-[58px] h-auto w-[min(var(--workspace-inspector-width),100vw)] shadow-[-20px_0_50px_rgba(20,31,26,.14)] max-[700px]:w-screen min-[1001px]:static min-[1001px]:z-auto min-[1001px]:h-full min-[1001px]:w-auto min-[1001px]:shadow-none";

  function cacheKey(format: DetailFormat, token: string) {
    return `${format}\0${token}`;
  }

  const storeCachedDetail = useCallback(
    (key: string, detail: CachedDetail, reportedSize?: string) => {
      const byteCost = measuredByteCost(detail.body, reportedSize);
      if (byteCost > DETAIL_CACHE_MAX_BYTES) return false;
      detailCache.current.delete(key);
      detailCache.current.set(key, { detail, byteCost });
      let cachedBytes = 0;
      for (const entry of detailCache.current.values()) cachedBytes += entry.byteCost;
      while (
        detailCache.current.size > DETAIL_CACHE_MAX_ENTRIES ||
        cachedBytes > DETAIL_CACHE_MAX_BYTES
      ) {
        // Background summaries yield their bodies before any actively inspected turn.
        // If open turns alone fill the budget, the oldest one can be explicitly reloaded.
        let oldest = detailCache.current.entries().next().value;
        for (const entry of detailCache.current.entries()) {
          if (!openTurnDetails.current.has(entry[0])) {
            oldest = entry;
            break;
          }
        }
        if (!oldest) break;
        detailCache.current.delete(oldest[0]);
        cachedBytes -= oldest[1].byteCost;
      }
      setCacheVersion((current) => current + 1);
      return true;
    },
    [],
  );

  const setTurnDetailOpen = useCallback((event: AgentEventDescriptorMsg, open: boolean) => {
    const key = `json\0${event.bodyToken}`;
    if (open) openTurnDetails.current.add(key);
    else openTurnDetails.current.delete(key);
  }, []);

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
          afterEventSequence: activeEventPage.nextCursor,
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
        setEventPage((current) =>
          mergeDescriptorPage(current, page, (event) => event.eventSequence, "newer"),
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
    detailCache.current.clear();
    traceSummaries.current.clear();
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
    if (!isAgentNode || !hasRunNode || !nodeId || !runId || tab !== "trace") return;

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
    isAgentNode,
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
    return boundDescriptors(bySequence, (event) => event.eventSequence, "newer", liveSequences);
  }, [activeEventPage.records, eventPageOrder, liveEvents]);

  const turns = useMemo(
    () => combinedEvents.filter((event) => event.eventKind === "iteration.recorded"),
    [combinedEvents],
  );

  const hydrateTraceTurn = useCallback(
    (event: AgentEventDescriptorMsg) => {
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
          const step = parseAgentTraceStepEvent(body);
          traceSummaries.current.set(key, summarizeAgentTraceStep(step));
          while (traceSummaries.current.size > DESCRIPTOR_WINDOW_SIZE) {
            const oldest = traceSummaries.current.keys().next().value;
            if (oldest === undefined) break;
            traceSummaries.current.delete(oldest);
          }
          if (!storeCachedDetail(key, { body, step }, event.sizeBytes)) {
            setDetailErrors((current) => ({
              ...current,
              [key]: {
                error: "Turn detail exceeds the browser detail limit.",
                retryable: false,
              },
            }));
          }
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted || detailGeneration.current !== generation) return;
          setDetailErrors((current) => ({
            ...current,
            [key]: {
              error: error instanceof Error ? error.message : "Turn detail unavailable",
              retryable: true,
            },
          }));
        })
        .finally(() => {
          if (controller.signal.aborted || detailGeneration.current !== generation) return;
          detailControllers.current.delete(controller);
          detailLoading.current.delete(key);
          setDetailLoadingVersion((current) => current + 1);
        });
    },
    [api, storeCachedDetail],
  );

  const traceTurnViews = useMemo<AgentTraceTurn[]>(
    () =>
      turns.map((event) => {
        const key = cacheKey("json", event.bodyToken);
        const cached = detailCache.current.get(key)?.detail;
        const summary = traceSummaries.current.get(key);
        const error = detailErrors[key];
        const isLoading = detailLoading.current.has(key);
        return {
          descriptor: event,
          summary: summary
            ? { status: "ready", step: summary }
            : error
              ? { status: "error", error: error.error }
              : isLoading
                ? { status: "loading" }
                : { status: "idle" },
          detail: cached
            ? { status: "ready", step: cached.step }
            : error
              ? { status: "error", ...error }
              : isLoading
                ? { status: "loading" }
                : { status: "idle" },
        };
      }),
    [cacheVersion, detailErrors, detailLoadingVersion, turns],
  );
  const lifecycleEvents = useMemo(
    () => combinedEvents.filter((event) => event.eventKind !== "iteration.recorded"),
    [combinedEvents],
  );

  useEffect(() => {
    if (tab !== "trace" || !following || !turns.length || !traceScrollElement.current) return;
    traceScrollElement.current.scrollTop = traceScrollElement.current.scrollHeight;
  }, [following, tab, turns.length]);

  if (!nodeId || (!run && !currentNodeExists) || (run && !isAgentNode)) return null;
  const activePageError = pageError?.key === pageKey ? pageError.value : undefined;
  const nodeDuration = node
    ? node.status === "running" && node.runningElapsedSeconds !== undefined
      ? formatSeconds(node.runningElapsedSeconds)
      : node.startedAt > 0 && node.endedAt >= node.startedAt
        ? formatSeconds(node.endedAt - node.startedAt)
        : undefined
    : undefined;
  const traceHeader = isAgentNode ? node?.trace?.header : undefined;
  let traceUsage: TraceLmUsage | undefined;
  if (traceHeader?.usageJson) {
    try {
      traceUsage = parseAgentTraceUsage(JSON.parse(traceHeader.usageJson) as unknown);
    } catch {
      traceUsage = undefined;
    }
  }
  const declaredModels = isUnknownRecord(workflowDeclaration?.model)
    ? workflowDeclaration.model
    : undefined;
  const declaredMainModel = declaredModelName(declaredModels?.main);
  const declaredSubModel = declaredModelName(declaredModels?.sub);
  const mainModel = traceHeader?.model;
  const subModel = traceHeader?.subModel;
  const headerDuration = traceHeader
    ? formatTraceDuration(traceHeader.durationMs)
    : nodeDuration;
  const nodeStatusClass =
    node?.status === "success"
      ? "status-success text-success"
      : node?.status === "failed"
        ? "status-failed text-failed"
        : "text-muted";
  const nodeName = run
    ? run.topology?.displayNames[nodeId] || node?.name || nodeId
    : workflow?.displayNames[nodeId] || nodeId;
  const definitionUnavailable = !workflow
    ? `${definitionLabel} is unavailable.`
    : !currentNodeExists
      ? `${definitionLabel} no longer exists.`
      : undefined;
  const instructions = workflowDeclaration?.instructions || "No instructions.";
  const closeButton = (
    <button
      type="button"
      className="icon-button grid size-[30px] shrink-0 cursor-pointer place-items-center rounded-[7px] border border-line bg-panel p-0 text-secondary hover:border-secondary hover:bg-panel hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid"
      onClick={closeInspector}
      aria-label="Close"
    >
      <X aria-hidden="true" className="size-4" strokeWidth={1.8} />
    </button>
  );
  const headerActions = (
    <div className="flex shrink-0 self-stretch flex-col items-end justify-between gap-3">
      {closeButton}
    </div>
  );

  return (
    <aside
      className={`inspector ${run ? "inspector-run" : "inspector-declaration"} z-30 flex min-h-0 min-w-0 flex-col overflow-hidden border-l border-line bg-panel ${panelLayout}`}
      aria-label={run ? "Run inspector" : isAgentNode ? "Node declaration" : "Node code"}
    >
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-5 pt-[19px] pb-3.5">
        <div className="min-w-0 flex-1">
          <span className="eyebrow block font-mono text-[9px] tracking-[.16em] text-acid uppercase">
            {isAgentNode ? "Agent" : "Step"}
            <span aria-hidden="true"> · </span>
            {run ? (
              <>
                Run <span className="normal-case">{runId}</span>
              </>
            ) : (
              "Current state"
            )}
          </span>
          <div className="mt-1 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
            <h2 className="m-0 min-w-0 text-lg [overflow-wrap:anywhere]">{nodeName}</h2>
            {node?.status && (
              <span className={`node-status font-mono text-[8px] uppercase ${nodeStatusClass}`}>
                {node.status}
              </span>
            )}
            {headerDuration && (
              <span className="font-mono text-[9px] text-muted">{headerDuration}</span>
            )}
          </div>
          {run && traceHeader && (
            <div className="mt-2 grid min-w-0 gap-0.5 font-mono text-[8px] leading-[1.5]">
              <p className="m-0 whitespace-normal text-secondary [overflow-wrap:anywhere]">
                <span className="mr-1.5 text-muted uppercase">Main</span>
                {mainModel ?? "—"}
                {traceUsage &&
                  ` · ${TOKEN_NUMBER_FORMAT.format(traceUsage.main.inputTokens)} in / ${TOKEN_NUMBER_FORMAT.format(traceUsage.main.outputTokens)} out · $${traceUsage.main.cost.toFixed(4)}`}
              </p>
              <p className="m-0 whitespace-normal text-secondary [overflow-wrap:anywhere]">
                <span className="mr-1.5 text-muted uppercase">Sub</span>
                {subModel ?? "—"}
                {traceUsage &&
                  ` · ${TOKEN_NUMBER_FORMAT.format(traceUsage.sub.inputTokens)} in / ${TOKEN_NUMBER_FORMAT.format(traceUsage.sub.outputTokens)} out · $${traceUsage.sub.cost.toFixed(4)}`}
              </p>
              <p className="m-0 text-muted">
                <span className="text-secondary">
                  {traceHeader.iterations} of {traceHeader.maxIterations}
                </span>{" "}
                iterations
              </p>
            </div>
          )}
          {node?.error && (
            <p
              className="mt-2 mb-0 line-clamp-3 text-[9px] text-danger [overflow-wrap:anywhere]"
              title={node.error}
            >
              {node.error}
            </p>
          )}
        </div>
        {headerActions}
      </header>
      {isAgentNode ? (
        run ? (
          <>
            <div
              className="inspector-tabs flex shrink-0 overflow-x-auto border-b border-line px-2.5"
              role="tablist"
              aria-label="Run agent detail views"
            >
              {(["trace", "io"] as const).map((item) => (
                <button
                  type="button"
                  role="tab"
                  id={`${tabsId}-${item}`}
                  aria-controls={`${tabsId}-panel`}
                  aria-selected={tab === item}
                  tabIndex={tab === item ? 0 : -1}
                  key={item}
                  className={`flex-[1_0_auto] cursor-pointer border-0 border-b-2 bg-transparent px-[9px] pt-[11px] pb-[9px] font-mono text-[9px] uppercase focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid ${tab === item ? "active border-acid text-acid" : "border-transparent text-muted"}`}
                  onClick={() => setSelectedTab(item)}
                  onKeyDown={(event) => {
                    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                    event.preventDefault();
                    const next: AgentTab =
                      event.key === "Home"
                        ? "trace"
                        : event.key === "End"
                          ? "io"
                          : item === "trace"
                            ? "io"
                            : "trace";
                    setSelectedTab(next);
                    document.getElementById(`${tabsId}-${next}`)?.focus();
                  }}
                >
                  {item === "trace" ? "Trace" : "Run I/O"}
                </button>
              ))}
            </div>
            <div
              className={`inspector-body inspector-body-full min-h-0 min-w-0 flex-1 ${tab === "trace" ? "overflow-hidden [&_h3]:text-[10px] [&_h3]:tracking-[.08em] [&_h3]:text-secondary [&_h3]:uppercase" : "overflow-auto px-5 pt-[18px] pb-[30px] [scrollbar-gutter:stable]"}`}
              role="tabpanel"
              id={`${tabsId}-panel`}
              aria-labelledby={`${tabsId}-${tab}`}
            >
              {tab === "io" ? (
                <div className="grid min-w-0 gap-6">
                  {(["inputs", "outputs"] as const).map((kind) => (
                    <section
                      key={kind}
                      aria-label={kind === "inputs" ? "Inputs" : "Outputs"}
                      className="min-w-0"
                    >
                      <h3 className="inspector-section-title">
                        {kind === "inputs" ? "Inputs" : "Outputs"}
                      </h3>
                      {runId ? (
                        <RetainedAgentValue
                          key={`${descriptorScope}\0${kind}`}
                          api={api}
                          kind={kind}
                          fields={historicalFieldSchemas?.[kind]}
                          schemaUnavailableMessage={`Historical ${kind === "inputs" ? "input" : "output"} schema unavailable for this run.`}
                          operatorInstanceId={operatorInstanceId}
                          asOfEventUlid={asOfEventUlid}
                          runId={runId}
                          nodeId={nodeId}
                          eventPageToken={eventPageToken}
                          liveEvents={liveEvents}
                          nodeStatus={node?.status}
                        />
                      ) : (
                        <InspectorFields
                          fields={historicalFieldSchemas?.[kind]}
                          unavailableMessage={`Historical ${kind === "inputs" ? "input" : "output"} schema unavailable for this run.`}
                        />
                      )}
                    </section>
                  ))}
                </div>
              ) : node?.trace ? (
                <AgentTraceExplorer
                  key={descriptorScope}
                  scopeKey={descriptorScope}
                  turns={traceTurnViews}
                  lifecycleEvents={lifecycleEvents}
                  running={node.status === "running"}
                  loading={pageLoading}
                  error={activePageError}
                  hasMore={Boolean(activeEventPage.nextPageToken)}
                  scrollRef={traceScrollElement}
                  onLoadTurn={hydrateTraceTurn}
                  onTurnDetailOpenChange={setTurnDetailOpen}
                  onLoadMore={loadMoreEvents}
                  onScroll={(element) => {
                    const distanceFromBottom =
                      element.scrollHeight - element.scrollTop - element.clientHeight;
                    if (distanceFromBottom > SCROLL_LOAD_THRESHOLD_PX) setFollowing(false);
                    if (
                      distanceFromBottom <= SCROLL_LOAD_THRESHOLD_PX &&
                      activeEventPage.nextPageToken
                    )
                      loadMoreEvents();
                  }}
                />
              ) : (
                <section
                  aria-label="Agent trace"
                  className="inspector-panel h-full min-w-0 overflow-auto px-5 pt-[18px] pb-[30px]"
                >
                  <p className="text-[11px] text-muted">
                    {node
                      ? "No structured agent trace is available for this node."
                      : "This node has no execution data yet."}
                  </p>
                </section>
              )}
            </div>
          </>
        ) : (
          <div className="inspector-definition inspector-body inspector-body-full min-h-0 min-w-0 flex-1 overflow-auto px-5 pt-[18px] pb-[30px] [scrollbar-gutter:stable]">
            <div className="grid min-w-0 gap-6">
              {definitionUnavailable ? (
                <p className="text-[11px] text-muted">{definitionUnavailable}</p>
              ) : workflowDeclaration ? (
                <>
                  <section>
                    <h3 className="inspector-section-title">Instructions</h3>
                    <Markdown className="instructions text-xs leading-[1.65] whitespace-normal text-secondary [&>:first-child]:mt-0 [&>:last-child]:mb-0">
                      {instructions}
                    </Markdown>
                  </section>
                  <section aria-label="Inputs and outputs" className="min-w-0">
                    <h3 className="inspector-section-title">Inputs & outputs</h3>
                    <div className="grid min-w-0 gap-5">
                      {(["inputs", "outputs"] as const).map((kind) => (
                        <section
                          key={kind}
                          aria-label={kind === "inputs" ? "Inputs" : "Outputs"}
                          className="min-w-0"
                        >
                          <h4 className="m-0 mb-2 font-mono text-[9px] tracking-[.08em] text-muted uppercase">
                            {kind === "inputs" ? "Inputs" : "Outputs"}
                          </h4>
                          <InspectorFields fields={workflowDeclaration[kind]} />
                        </section>
                      ))}
                    </div>
                  </section>
                  {(declaredMainModel || declaredSubModel) && (
                    <section aria-label="Models">
                      <h3 className="inspector-section-title">Models</h3>
                      <div className="grid min-w-0 gap-3">
                        {(
                          [
                            ["Main", declaredMainModel],
                            ["Sub", declaredSubModel],
                          ] as const
                        ).flatMap(([label, model]) =>
                          model
                            ? [
                                <div className="min-w-0" key={label}>
                                  <span className="block font-mono text-[8px] text-muted uppercase">
                                    {label}
                                  </span>
                                  <strong className="mt-0.5 block text-[10px] text-ink [overflow-wrap:anywhere]">
                                    {model}
                                  </strong>
                                </div>,
                              ]
                            : [],
                        )}
                      </div>
                    </section>
                  )}
                  <InspectorResources
                    key={`${selectionScope}\0${workflow?.workflowId}`}
                    declaration={workflowDeclaration}
                  />
                </>
              ) : (
                <p className="text-[11px] text-muted">
                  This node has no agent declaration metadata.
                </p>
              )}
            </div>
          </div>
        )
      ) : (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-canvas">
          {definitionUnavailable ? (
            <p className="px-5 text-[11px] text-muted">{definitionUnavailable}</p>
          ) : activeNodeSource?.status === "ready" ? (
            activeNodeSource.sourceCode !== undefined ? (
              <div className="inspector-body inspector-body-full min-h-0 min-w-0 flex-1 overflow-hidden">
                <PythonSource source={activeNodeSource.sourceCode} />
              </div>
            ) : (
              <p className="px-5 text-[11px] text-muted">
                Source code is unavailable for this node.
              </p>
            )
          ) : activeNodeSource?.status === "error" ? (
            <p className="px-5 text-[11px] text-muted" role="alert">
              Source code is unavailable: {activeNodeSource.error}
            </p>
          ) : (
            <p className="px-5 text-[11px] text-muted" role="status">
              Loading source code…
            </p>
          )}
        </div>
      )}
    </aside>
  );
}
