import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import type { OperatorApi } from "./api";
import { parseAgentDeclaration, parseAgentFieldSchemas } from "./GraphCanvas";
import {
  DescriptorPageOrder,
  type AgentEventDescriptorMsg,
  type FlowInfoMsg,
  type LogRecordDescriptorMsg,
  type NodeSnapshotMsg,
  type RunSnapshotMsg,
} from "./generated/operator";
import { isUnknownRecord } from "./guards";
import { ValueView } from "./ValueView";

interface InspectorProps {
  api: OperatorApi;
  workflow?: FlowInfoMsg;
  run?: RunSnapshotMsg;
  nodeId?: string;
  liveEvents?: AgentEventDescriptorMsg[];
  liveLogs?: LogRecordDescriptorMsg[];
  onClose: () => void;
}

type RunTab = "overview" | "inputs" | "output" | "trace" | "logs";

interface DescriptorPageState<T> {
  records: T[];
  nextPageToken: string;
  nextCursor: string;
}

interface ScopedEventSelection {
  scope: string;
  eventSequence: string;
}

interface ScopedValueEventSelection extends ScopedEventSelection {
  tab: "inputs" | "output";
}

interface ScopedLogSelection {
  scope: string;
  sequence: string;
  bodyToken: string;
}

interface ScopedResult<T> {
  key: string;
  value: T;
}

const DESCRIPTOR_PAGE_SIZE = 100;
const DESCRIPTOR_WINDOW_SIZE = 500;
const DETAIL_CACHE_MAX_ENTRIES = 8;
const DETAIL_CACHE_MAX_BYTES = 8 * 1024 * 1024;

const EMPTY_EVENTS: AgentEventDescriptorMsg[] = [];
const EMPTY_LOGS: LogRecordDescriptorMsg[] = [];
const EMPTY_EVENT_PAGE: DescriptorPageState<AgentEventDescriptorMsg> = {
  records: EMPTY_EVENTS,
  nextPageToken: "",
  nextCursor: "0",
};
const EMPTY_LOG_PAGE: DescriptorPageState<LogRecordDescriptorMsg> = {
  records: EMPTY_LOGS,
  nextPageToken: "",
  nextCursor: "0",
};

interface DetailCacheEntry {
  value: unknown;
  byteCost: number;
}

type DescriptorRetention = "older" | "newer";

function compareSequence(left: string, right: string) {
  if (left.length !== right.length) return left.length - right.length;
  return left < right ? -1 : left > right ? 1 : 0;
}
function boundDescriptors<T>(
  recordsBySequence: Map<string, T>,
  sequence: (record: T) => string,
  retention: DescriptorRetention,
  selectedSequence?: string,
  retainedSequences: Iterable<string> = [],
): T[] {
  const merged = [...recordsBySequence.values()].sort((left, right) =>
    compareSequence(sequence(left), sequence(right)),
  );
  if (merged.length <= DESCRIPTOR_WINDOW_SIZE) return merged;

  const retained = new Set(retainedSequences);
  if (selectedSequence) retained.add(selectedSequence);
  const retainedRecords: T[] = [];
  const availableRecords: T[] = [];
  for (const record of merged) {
    (retained.has(sequence(record)) ? retainedRecords : availableRecords).push(record);
  }
  const records =
    retainedRecords.length >= DESCRIPTOR_WINDOW_SIZE
      ? retainedRecords.slice(-DESCRIPTOR_WINDOW_SIZE)
      : [
          ...(retention === "newer"
            ? availableRecords.slice(-(DESCRIPTOR_WINDOW_SIZE - retainedRecords.length))
            : availableRecords.slice(0, DESCRIPTOR_WINDOW_SIZE - retainedRecords.length)),
          ...retainedRecords,
        ].sort((left, right) => compareSequence(sequence(left), sequence(right)));
  if (selectedSequence && !records.some((record) => sequence(record) === selectedSequence)) {
    const selected = recordsBySequence.get(selectedSequence);
    if (selected) {
      records.shift();
      records.push(selected);
      records.sort((left, right) => compareSequence(sequence(left), sequence(right)));
    }
  }
  return records;
}


function mergeDescriptorPage<T>(
  current: DescriptorPageState<T>,
  next: DescriptorPageState<T>,
  sequence: (record: T) => string,
  retention: DescriptorRetention,
  selectedSequence?: string,
): DescriptorPageState<T> {
  const recordsBySequence = new Map<string, T>();
  for (const record of current.records) recordsBySequence.set(sequence(record), record);
  for (const record of next.records) recordsBySequence.set(sequence(record), record);
  const records = boundDescriptors(recordsBySequence, sequence, retention, selectedSequence);
  return { ...next, records };
}

function descriptorByteCost(sizeBytes: string | undefined): number {
  const byteCost = Number(sizeBytes);
  return Number.isFinite(byteCost) && byteCost > 0 ? byteCost : 0;
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>;
}

function eventPayload(value: unknown): Record<string, unknown> | undefined {
  if (!isUnknownRecord(value)) return undefined;
  return isUnknownRecord(value.data) ? value.data : value;
}

export function Inspector({
  api,
  workflow,
  run,
  nodeId,
  liveEvents = EMPTY_EVENTS,
  liveLogs = EMPTY_LOGS,
  onClose,
}: InspectorProps) {
  const [tabSelection, setTabSelection] = useState<{
    scope: string;
    tab: RunTab;
  }>();
  const [eventPage, setEventPage] =
    useState<DescriptorPageState<AgentEventDescriptorMsg>>(EMPTY_EVENT_PAGE);
  const [logPage, setLogPage] =
    useState<DescriptorPageState<LogRecordDescriptorMsg>>(EMPTY_LOG_PAGE);
  const [selectedTraceEvent, setSelectedTraceEvent] = useState<ScopedEventSelection>();
  const [selectedValueEvent, setSelectedValueEvent] =
    useState<ScopedValueEventSelection>();
  const [selectedLog, setSelectedLog] = useState<ScopedLogSelection>();
  const [detailResult, setDetailResult] = useState<ScopedResult<unknown>>();
  const [detailError, setDetailError] = useState<ScopedResult<string>>();
  const [pageError, setPageError] = useState<ScopedResult<string>>();
  const [pageLoading, setPageLoading] = useState(false);
  const [following, setFollowing] = useState(true);
  const detailCache = useRef(new Map<string, DetailCacheEntry>());
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  const [logScrollElement, setLogScrollElement] = useState<HTMLDivElement | null>(null);
  const pageController = useRef<AbortController | undefined>(undefined);
  const pageGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const selectedEventSequenceRef = useRef<string | undefined>(undefined);
  const selectedLogSequenceRef = useRef<string | undefined>(undefined);
  const node: NodeSnapshotMsg | undefined = run?.nodes.find((item) => item.nodeId === nodeId);
  const runId = run?.summary?.runId;
  const operatorInstanceId = run?.operatorInstanceId ?? "";
  const asOfSequence = run?.asOfSequence ?? "";
  const eventPageToken = node?.eventPageToken ?? "";
  const logPageToken = run?.logPageToken ?? "";
  const hasRunNode = Boolean(run && node);
  const selectionScope = `${operatorInstanceId}\0${runId ?? ""}\0${nodeId ?? ""}`;
  const descriptorScope = `${selectionScope}\0${asOfSequence}\0${eventPageToken}\0${logPageToken}`;
  const tab =
    tabSelection?.scope === selectionScope ? tabSelection.tab : "overview";
  const pageKey = `${descriptorScope}\0${tab}`;
  const eventPageOrder =
    tab === "output" ? DescriptorPageOrder.NEWEST_FIRST : DescriptorPageOrder.FORWARD;
  const workflowDeclaration = run
    ? undefined
    : parseAgentDeclaration(workflow?.agentMetadataJson[nodeId ?? ""]);
  const runFieldSchemas = run
    ? parseAgentFieldSchemas(run.topology?.agentFieldSchemasJson[nodeId ?? ""])
    : undefined;

  useEffect(() => {
    setTabSelection({ scope: selectionScope, tab: "overview" });
  }, [selectionScope]);

  useEffect(() => {
    setEventPage(EMPTY_EVENT_PAGE);
    setLogPage(EMPTY_LOG_PAGE);
    setSelectedTraceEvent(undefined);
    setSelectedValueEvent(undefined);
    setSelectedLog(undefined);
    setDetailResult(undefined);
    setDetailError(undefined);
    setPageError(undefined);
    setFollowing(true);
    detailCache.current.clear();
  }, [api, descriptorScope]);

  useEffect(() => {
    pageController.current?.abort();
    const generation = ++pageGeneration.current;
    setPageError(undefined);
    setPageLoading(false);

    if (!hasRunNode || !nodeId || !runId || tab === "overview") return;

    if (tab === "logs") setLogPage(EMPTY_LOG_PAGE);
    else setEventPage(EMPTY_EVENT_PAGE);

    const controller = new AbortController();
    pageController.current = controller;
    setPageLoading(true);
    const request =
      tab === "logs"
        ? logPageToken
          ? api.listLogPage(
              {
                pageToken: logPageToken,
                afterSequence: "0",
                beforeSequence: "0",
                pageSize: DESCRIPTOR_PAGE_SIZE,
                nodeId,
                order: DescriptorPageOrder.NEWEST_FIRST,
                expectedOperatorInstanceId: operatorInstanceId,
                expectedAsOfSequence: asOfSequence,
              },
              controller.signal,
            )
          : undefined
        : eventPageToken
          ? api.listAgentEventPage(
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
          : undefined;
    if (!request) {
      setPageLoading(false);
      return () => pageController.current?.abort();
    }

    void request
      .then((page) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        if ("runId" in page) setEventPage(page);
        else setLogPage(page);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError({
          key: pageKey,
          value:
            error instanceof Error
              ? error.message
              : tab === "logs"
                ? "Logs unavailable"
                : "Events unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageLoading(false);
      });

    return () => pageController.current?.abort();
  }, [
    api,
    asOfSequence,
    descriptorScope,
    eventPageToken,
    eventPageOrder,
    hasRunNode,
    logPageToken,
    nodeId,
    operatorInstanceId,
    pageKey,
    runId,
    tab,
  ]);

  function loadMoreEvents() {
    if (!eventPage.nextPageToken || !nodeId || !runId || pageLoading) return;
    pageController.current?.abort();
    const generation = ++pageGeneration.current;
    const controller = new AbortController();
    pageController.current = controller;
    setPageError(undefined);
    setPageLoading(true);
    void api
      .listAgentEventPage(
        {
          pageToken: eventPage.nextPageToken,
          afterEventSequence:
            eventPageOrder === DescriptorPageOrder.FORWARD ? eventPage.nextCursor : "0",
          beforeEventSequence:
            eventPageOrder === DescriptorPageOrder.NEWEST_FIRST ? eventPage.nextCursor : "0",
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
            selectedEventSequenceRef.current ??
              (tab === "output"
                ? current.records.find((event) => event.eventKind === "run.succeeded")
                    ?.eventSequence
                : undefined),
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
        setPageLoading(false);
      });
  }

  function loadOlderLogs() {
    if (!logPage.nextPageToken || !nodeId || pageLoading) return;
    pageController.current?.abort();
    const generation = ++pageGeneration.current;
    const controller = new AbortController();
    pageController.current = controller;
    setPageError(undefined);
    setPageLoading(true);
    void api
      .listLogPage(
        {
          pageToken: logPage.nextPageToken,
          afterSequence: "0",
          beforeSequence: logPage.nextCursor,
          pageSize: DESCRIPTOR_PAGE_SIZE,
          nodeId,
          order: DescriptorPageOrder.NEWEST_FIRST,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfSequence: asOfSequence,
        },
        controller.signal,
      )
      .then((page) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setLogPage((current) =>
          mergeDescriptorPage(
            current,
            page,
            (entry) => entry.sequence,
            "older",
            selectedLogSequenceRef.current,
          ),
        );
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError({
          key: pageKey,
          value: error instanceof Error ? error.message : "Logs unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageLoading(false);
      });
  }

  const selectedEventSequence =
    tab === "trace"
      ? selectedTraceEvent?.scope === descriptorScope
        ? selectedTraceEvent.eventSequence
        : undefined
      : tab === "inputs" || tab === "output"
        ? selectedValueEvent?.scope === descriptorScope &&
          selectedValueEvent.tab === tab
          ? selectedValueEvent.eventSequence
          : undefined
        : undefined;
  const selectedLogSelection =
    selectedLog?.scope === descriptorScope ? selectedLog : undefined;
  const combinedEvents = useMemo(() => {
    const bySequence = new Map<string, AgentEventDescriptorMsg>();
    const liveSequences = new Set<string>();
    for (const event of eventPage.records) bySequence.set(event.eventSequence, event);
    for (const event of liveEvents) {
      bySequence.set(event.eventSequence, event);
      liveSequences.add(event.eventSequence);
    }
    return boundDescriptors(
      bySequence,
      (event) => event.eventSequence,
      eventPageOrder === DescriptorPageOrder.FORWARD ? "newer" : "older",
      selectedEventSequence,
      liveSequences,
    );
  }, [eventPage.records, eventPageOrder, liveEvents, selectedEventSequence]);
  const turns = useMemo(
    () => combinedEvents.filter((event) => event.eventKind === "iteration.recorded"),
    [combinedEvents],
  );
  const combinedLogs = useMemo(() => {
    const bySequence = new Map<string, LogRecordDescriptorMsg>();
    const liveSequences = new Set<string>();
    for (const entry of logPage.records) {
      if (entry.nodeId === nodeId) bySequence.set(entry.sequence, entry);
    }
    for (const entry of liveLogs) {
      if (entry.nodeId === nodeId) {
        bySequence.set(entry.sequence, entry);
        liveSequences.add(entry.sequence);
      }
    }
    return boundDescriptors(
      bySequence,
      (entry) => entry.sequence,
      "older",
      selectedLogSelection?.sequence,
      liveSequences,
    ).sort((left, right) => compareSequence(right.sequence, left.sequence));
  }, [liveLogs, logPage.records, nodeId, selectedLogSelection?.sequence]);
  const traceUsage = useMemo<unknown>(() => {
    const usageJson = node?.trace?.header?.usageJson;
    return usageJson ? JSON.parse(usageJson) : undefined;
  }, [node?.trace?.header?.usageJson]);
  const traceTelemetry = useMemo<unknown>(() => {
    const telemetryJson = node?.trace?.header?.telemetryJson;
    return telemetryJson ? JSON.parse(telemetryJson) : undefined;
  }, [node?.trace?.header?.telemetryJson]);
  const declaredFields =
    tab === "inputs"
      ? runFieldSchemas?.inputs
      : tab === "output"
        ? runFieldSchemas?.outputs
        : undefined;
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollElement,
    estimateSize: () => 64,
    overscan: 6,
    initialRect: { width: 0, height: 220 },
  });
  const logVirtualizer = useVirtualizer({
    count: combinedLogs.length,
    getScrollElement: () => logScrollElement,
    estimateSize: () => 34,
    overscan: 8,
    initialRect: { width: 0, height: 220 },
  });

  const selectedEventDescriptor = combinedEvents.find(
    (event) => event.eventSequence === selectedEventSequence,
  );
  const selectedLogToken = selectedLogSelection?.bodyToken;
  const selectedLogDescriptor = combinedLogs.find(
    (entry) => entry.sequence === selectedLogSelection?.sequence,
  );
  selectedEventSequenceRef.current = selectedEventSequence;
  selectedLogSequenceRef.current = selectedLogSelection?.sequence;
  const detailToken =
    tab === "logs"
      ? selectedLogToken
      : tab === "trace" || tab === "inputs" || tab === "output"
        ? selectedEventDescriptor?.bodyToken
        : undefined;
  const detailFormat = tab === "logs" ? "text" : "json";
  const detailKey = detailToken
    ? `${descriptorScope}\0${tab}\0${detailFormat}\0${detailToken}`
    : undefined;
  const detail =
    detailKey !== undefined &&
    detailResult !== undefined &&
    detailResult.key === detailKey
      ? detailResult.value
      : undefined;
  const activeError =
    detailKey !== undefined &&
    detailError !== undefined &&
    detailError.key === detailKey
      ? detailError.value
      : pageError?.key === pageKey
        ? pageError.value
        : undefined;

  useEffect(() => {
    if (tab !== "trace" || !following || !turns.length) return;
    const eventSequence = turns.at(-1)!.eventSequence;
    setSelectedTraceEvent((current) =>
      current?.scope === descriptorScope && current.eventSequence === eventSequence
        ? current
        : { scope: descriptorScope, eventSequence },
    );
    if (scrollElement) virtualizer.scrollToIndex(turns.length - 1, { align: "end" });
  }, [descriptorScope, following, scrollElement, tab, turns, virtualizer.scrollToIndex]);

  useEffect(() => {
    const generation = ++detailGeneration.current;
    setDetailError(undefined);
    if (!detailKey || !detailToken) return;

    const cacheKey = `${detailFormat}\0${detailToken}`;
    const cached = detailCache.current.get(cacheKey);
    if (cached !== undefined) {
      detailCache.current.delete(cacheKey);
      detailCache.current.set(cacheKey, cached);
      setDetailResult({ key: detailKey, value: cached.value });
      return;
    }

    const controller = new AbortController();
    setDetailResult(undefined);
    const request =
      detailFormat === "text"
        ? api.readTextDetail(detailToken, controller.signal)
        : api.readJsonDetail(detailToken, controller.signal);
    void request
      .then((body) => {
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        const byteCost = descriptorByteCost(
          detailFormat === "text"
            ? selectedLogDescriptor?.sizeBytes
            : selectedEventDescriptor?.sizeBytes,
        );
        if (byteCost <= DETAIL_CACHE_MAX_BYTES) {
          detailCache.current.delete(cacheKey);
          detailCache.current.set(cacheKey, { value: body, byteCost });
          let cachedBytes = 0;
          for (const entry of detailCache.current.values()) cachedBytes += entry.byteCost;
          while (
            detailCache.current.size > DETAIL_CACHE_MAX_ENTRIES ||
            cachedBytes > DETAIL_CACHE_MAX_BYTES
          ) {
            const oldest = detailCache.current.entries().next().value as
              | [string, DetailCacheEntry]
              | undefined;
            if (oldest === undefined) break;
            detailCache.current.delete(oldest[0]);
            cachedBytes -= oldest[1].byteCost;
          }
        }
        setDetailResult({ key: detailKey, value: body });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        setDetailError({
          key: detailKey,
          value: error instanceof Error ? error.message : "Detail unavailable",
        });
      });
    return () => controller.abort();
  }, [
    api,
    selectedLogDescriptor?.sizeBytes,
    detailFormat,
    detailKey,
    detailToken,
    selectedEventDescriptor?.sizeBytes,
  ]);

  useEffect(() => {
    if (tab !== "inputs" && tab !== "output") return;
    const kind = tab === "inputs" ? "run.started" : "run.succeeded";
    const descriptor = [...combinedEvents]
      .reverse()
      .find((event) => event.eventKind === kind);
    setSelectedValueEvent(
      descriptor
        ? { scope: descriptorScope, eventSequence: descriptor.eventSequence, tab }
        : undefined,
    );
  }, [combinedEvents, descriptorScope, tab]);

  if (!run && workflow && nodeId) {
    return (
      <aside className="inspector" aria-label="Node declaration">
        <header>
          <div>
            <span className="eyebrow">Declaration</span>
            <h2>{workflow.displayNames[nodeId] || nodeId}</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        {workflowDeclaration ? (
          <div className="inspector-body declaration">
            <section>
              <h3>Instructions</h3>
              <p className="instructions">
                {workflowDeclaration.instructions || "No instructions"}
              </p>
            </section>
            <section className="signature-columns">
              <div>
                <h3>Inputs</h3>
                {workflowDeclaration.inputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    <p>{field.description}</p>
                  </div>
                ))}
              </div>
              <div>
                <h3>Outputs</h3>
                {workflowDeclaration.outputs.map((field) => (
                  <div className="field-detail" key={field.name}>
                    <strong>{field.name}</strong>
                    <code>{field.type}</code>
                    <p>{field.description}</p>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <h3>Runtime</h3>
              <JsonBlock value={workflowDeclaration.runtime} />
            </section>
            <section>
              <h3>Models</h3>
              <JsonBlock value={workflowDeclaration.model} />
            </section>
            <section>
              <h3>Skills & tools</h3>
              <JsonBlock
                value={{
                  skills: workflowDeclaration.skills,
                  tools: workflowDeclaration.tools,
                }}
              />
            </section>
          </div>
        ) : (
          <p className="empty-copy">This node has no agent declaration metadata.</p>
        )}
      </aside>
    );
  }

  if (!run || !node) return null;
  const selectedPayload = eventPayload(detail);
  const valueKey = tab === "inputs" ? "inputs" : "outputs";

  return (
    <aside className="inspector" aria-label="Run inspector">
      <header>
        <div>
          <span className="eyebrow">Historical execution</span>
          <h2>{node.name}</h2>
          <span className={`status-pill status-${node.status}`}>{node.status}</span>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>
      <nav className="inspector-tabs" aria-label="Run detail views">
        {(["overview", "inputs", "output", "trace", "logs"] as RunTab[]).map((item) => (
          <button
            type="button"
            key={item}
            className={tab === item ? "active" : ""}
            onClick={() => setTabSelection({ scope: selectionScope, tab: item })}
          >
            {item}
          </button>
        ))}
      </nav>
      <div className="inspector-body">
        {activeError && <p className="error-banner">{activeError}</p>}
        {tab === "overview" && (
          <>
            <section className="metric-grid">
              <div><small>Status</small><strong>{node.status}</strong></div>
              <div><small>Revision</small><strong>{node.revision}</strong></div>
              <div><small>Started</small><strong>{node.startedAt ? "yes" : "—"}</strong></div>
              <div>
                <small>Duration</small>
                <strong>
                  {node.startedAt && node.endedAt
                    ? `${Math.max(0, node.endedAt - node.startedAt).toFixed(2)}s`
                    : "—"}
                </strong>
              </div>
            </section>
            {node.error && <p className="node-failure">{node.error}</p>}
            {node.trace && (
              <section>
                <h3>Trace header</h3>
                <dl className="trace-header">
                  <div><dt>Status</dt><dd>{node.trace.status}</dd></div>
                  <div><dt>Events</dt><dd>{node.trace.eventCount}</dd></div>
                  <div><dt>Size</dt><dd>{node.trace.sizeBytes} B</dd></div>
                  <div><dt>Complete</dt><dd>{node.trace.complete ? "yes" : "no"}</dd></div>
                  {node.trace.header && (
                    <>
                      <div><dt>Model</dt><dd>{node.trace.header.model}</dd></div>
                      <div>
                        <dt>Iterations</dt>
                        <dd>
                          {node.trace.header.iterations}/{node.trace.header.maxIterations}
                        </dd>
                      </div>
                      <div><dt>Duration</dt><dd>{node.trace.header.durationMs} ms</dd></div>
                    </>
                  )}
                </dl>
                {traceUsage !== undefined && (
                  <div className="trace-summary">
                    <h3>Usage</h3>
                    <ValueView value={traceUsage} />
                  </div>
                )}
                {traceTelemetry !== undefined && (
                  <div className="trace-summary">
                    <h3>Telemetry</h3>
                    <ValueView value={traceTelemetry} />
                  </div>
                )}
              </section>
            )}
          </>
        )}
        {(tab === "inputs" || tab === "output") && (
          <section>
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
            {selectedPayload && valueKey in selectedPayload ? (
              <ValueView value={selectedPayload[valueKey]} />
            ) : (
              <p className="empty-copy">
                No retained {tab} {tab === "output" ? "is" : "are"} available.
              </p>
            )}
            {eventPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action"
                disabled={pageLoading}
                aria-busy={pageLoading}
                onClick={loadMoreEvents}
              >
                Load more events
              </button>
            )}
          </section>
        )}
        {tab === "trace" && (
          <section className="trace-layout">
            <div className="trace-toolbar">
              <div>
                <h3>RunTrace</h3>
                <span>{turns.length} complete turns</span>
              </div>
              <div className="trace-actions">
                {eventPage.nextPageToken && (
                  <button
                    type="button"
                    className="descriptor-page-action"
                    disabled={pageLoading}
                    aria-busy={pageLoading}
                    onClick={loadMoreEvents}
                  >
                    Load more events
                  </button>
                )}
                <button
                  type="button"
                  className={following ? "toggle active" : "toggle"}
                  onClick={() => setFollowing((value) => !value)}
                >
                  {following ? "Following live" : "Follow latest"}
                </button>
              </div>
            </div>
            <div className="turn-list" ref={setScrollElement}>
              <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
                {virtualizer.getVirtualItems().map((row) => {
                  const event = turns[row.index];
                  return (
                    <button
                      type="button"
                      key={event.eventSequence}
                      className={`turn-row ${selectedEventSequence === event.eventSequence ? "active" : ""} ${event.error ? "failed" : ""}`}
                      style={{ transform: `translateY(${row.start}px)` }}
                      onClick={() => {
                        setFollowing(false);
                        setSelectedTraceEvent({
                          scope: descriptorScope,
                          eventSequence: event.eventSequence,
                        });
                      }}
                    >
                      <strong>Turn {event.iteration ?? row.index + 1}</strong>
                      <span>{event.durationMs ? `${event.durationMs} ms` : "—"}</span>
                      <small>{event.toolCount} tools · {event.predictCount} predicts</small>
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="turn-detail">
              {detail !== undefined ? <ValueView value={detail} /> : <p className="empty-copy">Select a turn.</p>}
            </div>
          </section>
        )}
        {tab === "logs" && (
          <section>
            <h3>Node logs</h3>
            <div className="log-list" ref={setLogScrollElement}>
              <div style={{ height: logVirtualizer.getTotalSize(), position: "relative" }}>
                {logVirtualizer.getVirtualItems().map((row) => {
                  const entry = combinedLogs[row.index];
                  return (
                    <button
                      type="button"
                      className="log-row"
                      key={entry.sequence}
                      style={{ transform: `translateY(${row.start}px)` }}
                      onClick={() => {
                        setSelectedLog({
                          scope: descriptorScope,
                          sequence: entry.sequence,
                          bodyToken: entry.bodyToken,
                        });
                      }}
                    >
                      <span className={`log-level level-${entry.level}`}>{entry.level}</span>
                      <time>{new Date(entry.timestamp * 1000).toLocaleTimeString()}</time>
                      <span>#{entry.sequence}</span>
                    </button>
                  );
                })}
              </div>
            </div>
            {logPage.nextPageToken && (
              <button
                type="button"
                className="descriptor-page-action"
                disabled={pageLoading}
                aria-busy={pageLoading}
                onClick={loadOlderLogs}
              >
                Load older logs
              </button>
            )}
            {detail !== undefined && <ValueView value={detail} />}
          </section>
        )}
      </div>
    </aside>
  );
}
