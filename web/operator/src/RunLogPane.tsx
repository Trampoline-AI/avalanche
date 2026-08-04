import { useVirtualizer } from "@tanstack/react-virtual";
import {
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { OperatorApi } from "./api";
import {
  boundDescriptors,
  compareSequence,
  DESCRIPTOR_PAGE_SIZE,
  DETAIL_CACHE_MAX_BYTES,
  type DescriptorPageState,
  measuredByteCost,
  mergeDescriptorPage,
  SCROLL_LOAD_THRESHOLD_PX,
} from "./detailProjection";
import {
  DescriptorPageOrder,
  type LogRecordDescriptorMsg,
  type RunSnapshotMsg,
} from "./generated/operator";

const LOG_DECODE_BATCH_SIZE = 20;
const LOG_ROW_ESTIMATE_PX = 48;
const LOG_ROW_OVERSCAN = 12;
const LOG_PANE_MIN_HEIGHT_PX = 140;
const LOG_PANE_DEFAULT_HEIGHT_PX = 260;
const LOG_PANE_DEFAULT_MAX_HEIGHT_PX = 720;
const LOG_PANE_KEYBOARD_STEP_PX = 16;
const EMPTY_LOGS: LogRecordDescriptorMsg[] = [];
const EMPTY_LOG_PAGE: DescriptorPageState<LogRecordDescriptorMsg> = {
  records: EMPTY_LOGS,
  nextPageToken: "",
  nextCursor: "0",
};

interface RunLogPaneProps {
  api: OperatorApi;
  run: RunSnapshotMsg;
  nodeId?: string;
  liveLogs?: LogRecordDescriptorMsg[];
  onSelectNode: (nodeId: string) => void;
}

interface ScrollAnchor {
  height: number;
  top: number;
}

interface ResizeStart {
  clientY: number;
  height: number;
}

function maximumPaneHeight(element: HTMLElement | null) {
  const parentHeight = element?.parentElement?.clientHeight ?? 0;
  const availableHeight = parentHeight > 0 ? parentHeight : window.innerHeight;
  return Math.max(LOG_PANE_MIN_HEIGHT_PX, Math.floor(availableHeight * 0.75));
}

function boundedPaneHeight(height: number, maximum: number) {
  return Math.min(maximum, Math.max(LOG_PANE_MIN_HEIGHT_PX, height));
}

function logTimestamp(timestamp: number) {
  if (!Number.isFinite(timestamp)) return "--:--:--.---";
  return new Date(timestamp * 1000).toISOString().slice(11, 23);
}

export function RunLogPane({
  api,
  run,
  nodeId,
  liveLogs = EMPTY_LOGS,
  onSelectNode,
}: RunLogPaneProps) {
  const [expanded, setExpanded] = useState(true);
  const [following, setFollowing] = useState(true);
  const [paneHeight, setPaneHeight] = useState(LOG_PANE_DEFAULT_HEIGHT_PX);
  const [maximumHeight, setMaximumHeight] = useState(LOG_PANE_DEFAULT_MAX_HEIGHT_PX);
  const [page, setPage] = useState<DescriptorPageState<LogRecordDescriptorMsg>>(EMPTY_LOG_PAGE);
  const [pageScope, setPageScope] = useState<string>();
  const [pageLoading, setPageLoading] = useState(false);
  const [pageError, setPageError] = useState<string>();
  const [logBodies, setLogBodies] = useState<Map<string, string>>(() => new Map());
  const [decodePending, setDecodePending] = useState(false);
  const [decodeError, setDecodeError] = useState<string>();
  const [decodeVersion, setDecodeVersion] = useState(0);

  const pageController = useRef<AbortController | null>(null);
  const pageGeneration = useRef(0);
  const pageRequestInFlight = useRef(false);
  const decodeController = useRef<AbortController | null>(null);
  const decodeGeneration = useRef(0);
  const decodeActive = useRef(false);
  const loadingTokens = useRef(new Set<string>());
  const droppedTokens = useRef(new Set<string>());
  const combinedLogsRef = useRef<LogRecordDescriptorMsg[]>([]);
  const scrollElement = useRef<HTMLDivElement>(null);
  const pendingScrollAnchor = useRef<ScrollAnchor | undefined>(undefined);
  const paneElement = useRef<HTMLElement>(null);
  const resizeStart = useRef<ResizeStart | undefined>(undefined);

  const runId = run.summary?.runId ?? "";
  const operatorInstanceId = run.operatorInstanceId;
  const asOfSequence = run.asOfSequence;
  const pageToken = run.logPageToken;
  const selectedRunNode = run.nodes.find((candidate) => candidate.nodeId === nodeId);
  const exactLogNodeId = selectedRunNode?.name ?? nodeId ?? "";
  const descriptorScope = `${operatorInstanceId}\0${runId}\0${asOfSequence}\0${pageToken}\0${exactLogNodeId}`;
  const activePage = pageScope === descriptorScope ? page : EMPTY_LOG_PAGE;

  const abortDecoding = useCallback(() => {
    decodeGeneration.current += 1;
    decodeController.current?.abort();
    decodeController.current = null;
    decodeActive.current = false;
    loadingTokens.current.clear();
  }, []);

  useEffect(
    () => () => {
      pageController.current?.abort();
      abortDecoding();
    },
    [abortDecoding],
  );

  useLayoutEffect(() => {
    const updateBounds = () => {
      const maximum = maximumPaneHeight(paneElement.current);
      setMaximumHeight(maximum);
      setPaneHeight((current) => boundedPaneHeight(current, maximum));
    };
    updateBounds();
    window.addEventListener("resize", updateBounds);
    return () => window.removeEventListener("resize", updateBounds);
  }, []);

  useEffect(() => {
    pageController.current?.abort();
    pageRequestInFlight.current = false;
    abortDecoding();
    const generation = ++pageGeneration.current;
    setPage(EMPTY_LOG_PAGE);
    setPageScope(undefined);
    setPageLoading(false);
    setPageError(undefined);
    setLogBodies(new Map());
    setDecodePending(false);
    setDecodeError(undefined);
    droppedTokens.current.clear();
    pendingScrollAnchor.current = undefined;
    setFollowing(true);
    if (!expanded) return;

    const controller = new AbortController();
    pageController.current = controller;
    if (!pageToken) {
      setPageScope(descriptorScope);
      return () => controller.abort();
    }

    pageRequestInFlight.current = true;
    setPageLoading(true);
    void api
      .listLogPage(
        {
          pageToken,
          afterSequence: "0",
          beforeSequence: "0",
          pageSize: DESCRIPTOR_PAGE_SIZE,
          nodeId: exactLogNodeId,
          order: DescriptorPageOrder.NEWEST_FIRST,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfSequence: asOfSequence,
        },
        controller.signal,
      )
      .then((next) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPage(next);
        setPageScope(descriptorScope);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError(error instanceof Error ? error.message : "Logs unavailable");
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
    abortDecoding,
    api,
    asOfSequence,
    descriptorScope,
    exactLogNodeId,
    expanded,
    operatorInstanceId,
    pageToken,
  ]);

  const combinedLogs = useMemo(() => {
    const recordsBySequence = new Map<string, LogRecordDescriptorMsg>();
    const liveSequences = new Set<string>();
    for (const entry of activePage.records) {
      if (!exactLogNodeId || entry.nodeId === exactLogNodeId) {
        recordsBySequence.set(entry.sequence, entry);
      }
    }
    for (const entry of liveLogs) {
      if (!exactLogNodeId || entry.nodeId === exactLogNodeId) {
        recordsBySequence.set(entry.sequence, entry);
        liveSequences.add(entry.sequence);
      }
    }
    return boundDescriptors(
      recordsBySequence,
      (entry) => entry.sequence,
      "older",
      liveSequences,
    ).sort((left, right) => compareSequence(left.sequence, right.sequence));
  }, [activePage.records, exactLogNodeId, liveLogs]);
  combinedLogsRef.current = combinedLogs;

  const loadOlderLogs = useCallback(() => {
    if (!activePage.nextPageToken || pageRequestInFlight.current) return;
    pageController.current?.abort();
    const generation = ++pageGeneration.current;
    const controller = new AbortController();
    pageController.current = controller;
    pageRequestInFlight.current = true;
    setPageError(undefined);
    setPageLoading(true);
    setFollowing(false);
    const element = scrollElement.current;
    pendingScrollAnchor.current = element
      ? { height: element.scrollHeight, top: element.scrollTop }
      : undefined;
    void api
      .listLogPage(
        {
          pageToken: activePage.nextPageToken,
          afterSequence: "0",
          beforeSequence: activePage.nextCursor,
          pageSize: DESCRIPTOR_PAGE_SIZE,
          nodeId: exactLogNodeId,
          order: DescriptorPageOrder.NEWEST_FIRST,
          expectedOperatorInstanceId: operatorInstanceId,
          expectedAsOfSequence: asOfSequence,
        },
        controller.signal,
      )
      .then((next) => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPage((current) =>
          mergeDescriptorPage(current, next, (entry) => entry.sequence, "older"),
        );
      })
      .catch((error: unknown) => {
        pendingScrollAnchor.current = undefined;
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        setPageError(error instanceof Error ? error.message : "Logs unavailable");
      })
      .finally(() => {
        if (controller.signal.aborted || pageGeneration.current !== generation) return;
        pageRequestInFlight.current = false;
        setPageLoading(false);
      });
  }, [
    activePage.nextCursor,
    activePage.nextPageToken,
    api,
    asOfSequence,
    exactLogNodeId,
    operatorInstanceId,
  ]);

  useLayoutEffect(() => {
    const anchor = pendingScrollAnchor.current;
    const element = scrollElement.current;
    if (!anchor || !element) return;
    element.scrollTop = anchor.top + element.scrollHeight - anchor.height;
    pendingScrollAnchor.current = undefined;
  }, [combinedLogs.length]);

  useEffect(() => {
    if (!expanded || !combinedLogs.length || decodeActive.current) return;
    const missing = combinedLogs
      .filter(
        (entry) =>
          !logBodies.has(entry.bodyToken) &&
          !loadingTokens.current.has(entry.bodyToken) &&
          !droppedTokens.current.has(entry.bodyToken),
      )
      .slice(0, LOG_DECODE_BATCH_SIZE);
    if (!missing.length) return;

    const generation = decodeGeneration.current;
    const controller = new AbortController();
    decodeController.current = controller;
    decodeActive.current = true;
    for (const entry of missing) loadingTokens.current.add(entry.bodyToken);
    setDecodePending(true);
    const requests = missing.map(async (entry) => {
      try {
        const body = await api.readTextDetail(entry.bodyToken, controller.signal);
        return { entry, body };
      } catch (error: unknown) {
        return { entry, error };
      }
    });
    void Promise.all(requests)
      .then((results) => {
        if (controller.signal.aborted || decodeGeneration.current !== generation) return;
        const failed = results.find((result) => "error" in result);
        if (failed && "error" in failed) {
          setDecodeError(
            failed.error instanceof Error ? failed.error.message : "Log text unavailable",
          );
        }
        const oversizedRecord = results.some(
          (result) =>
            "body" in result &&
            typeof result.body === "string" &&
            measuredByteCost(result.body, result.entry.sizeBytes) > DETAIL_CACHE_MAX_BYTES,
        );
        if (oversizedRecord) setDecodeError("A log record exceeds the browser detail limit.");
        setLogBodies((current) => {
          const next = new Map(current);
          for (const result of results) {
            if (!("body" in result) || typeof result.body !== "string") {
              droppedTokens.current.add(result.entry.bodyToken);
              continue;
            }
            const byteCost = measuredByteCost(result.body, result.entry.sizeBytes);
            if (byteCost > DETAIL_CACHE_MAX_BYTES) {
              droppedTokens.current.add(result.entry.bodyToken);
              continue;
            }
            next.set(result.entry.bodyToken, result.body);
          }
          const retainedDescriptors = new Map(
            combinedLogsRef.current.map((entry) => [entry.bodyToken, entry]),
          );
          for (const token of next.keys()) {
            if (!retainedDescriptors.has(token)) next.delete(token);
          }
          for (const token of droppedTokens.current) {
            if (!retainedDescriptors.has(token)) droppedTokens.current.delete(token);
          }
          let retainedBytes = 0;
          for (const [token, body] of next) {
            retainedBytes += measuredByteCost(body, retainedDescriptors.get(token)?.sizeBytes);
          }
          for (const entry of combinedLogsRef.current) {
            if (retainedBytes <= DETAIL_CACHE_MAX_BYTES) break;
            const body = next.get(entry.bodyToken);
            if (body === undefined) continue;
            next.delete(entry.bodyToken);
            droppedTokens.current.add(entry.bodyToken);
            retainedBytes -= measuredByteCost(body, entry.sizeBytes);
          }
          return next;
        });
      })
      .finally(() => {
        for (const entry of missing) loadingTokens.current.delete(entry.bodyToken);
        if (decodeController.current !== controller) return;
        decodeController.current = null;
        decodeActive.current = false;
        if (controller.signal.aborted || decodeGeneration.current !== generation) return;
        setDecodePending(false);
        setDecodeVersion((current) => current + 1);
      });
  }, [api, combinedLogs, decodeVersion, expanded, logBodies]);

  useEffect(() => {
    const element = scrollElement.current;
    if (!expanded || !following || !element) return;
    element.scrollTop = element.scrollHeight;
  }, [combinedLogs.length, decodeVersion, expanded, following]);

  const enableAutoScroll = useCallback(() => {
    const element = scrollElement.current;
    if (element) element.scrollTop = element.scrollHeight;
    setFollowing(true);
  }, []);

  const endResize = (event: PointerEvent<HTMLDivElement>) => {
    if (!resizeStart.current) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    resizeStart.current = undefined;
  };

  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const maximum = maximumPaneHeight(paneElement.current);
    setMaximumHeight(maximum);
    let nextHeight: number | undefined;
    if (event.key === "ArrowUp") nextHeight = paneHeight + LOG_PANE_KEYBOARD_STEP_PX;
    if (event.key === "ArrowDown") nextHeight = paneHeight - LOG_PANE_KEYBOARD_STEP_PX;
    if (event.key === "Home") nextHeight = LOG_PANE_MIN_HEIGHT_PX;
    if (event.key === "End") nextHeight = maximum;
    if (nextHeight === undefined) return;
    event.preventDefault();
    setPaneHeight(boundedPaneHeight(nextHeight, maximum));
  };

  const virtualizer = useVirtualizer({
    count: combinedLogs.length,
    getScrollElement: () => scrollElement.current,
    estimateSize: () => LOG_ROW_ESTIMATE_PX,
    getItemKey: (index) => combinedLogs[index].sequence,
    overscan: LOG_ROW_OVERSCAN,
  });
  const nodeDisplayNames = run.topology?.displayNames ?? {};
  const graphNodeByLogIdentity = useMemo(() => {
    const nodes = new Map<string, (typeof run.nodes)[number] | undefined>();
    for (const candidate of run.nodes) {
      nodes.set(candidate.name, nodes.has(candidate.name) ? undefined : candidate);
    }
    return nodes;
  }, [run.nodes]);
  const scopeLabel = nodeId
    ? nodeDisplayNames[nodeId] && nodeDisplayNames[nodeId] !== nodeId
      ? `${nodeDisplayNames[nodeId]} · ${nodeId}`
      : nodeId
    : "All steps";

  const paneStyle = expanded
    ? ({ flexBasis: `${paneHeight}px` } satisfies CSSProperties)
    : undefined;

  return (
    <section
      ref={paneElement}
      className={`run-log-pane relative z-[6] grid min-h-0 min-w-0 flex-[0_0_35px] grid-rows-[35px_minmax(0,1fr)] border-t border-line bg-[rgba(255,255,255,.98)] shadow-[0_-5px_18px_rgba(20,31,26,.08)] ${expanded ? "expanded min-h-[140px] max-h-[75%]" : ""}`}
      style={paneStyle}
      aria-label="Run logs"
    >
      {expanded && (
        <div
          className="run-log-resizer absolute -top-1 right-0 left-0 z-[2] h-[9px] cursor-ns-resize touch-none select-none after:absolute after:top-[3px] after:right-0 after:left-0 after:h-0.5 after:bg-line after:content-[''] after:transition-colors after:duration-150 hover:after:bg-acid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid focus-visible:after:bg-acid"
          role="separator"
          aria-label="Resize logs"
          aria-controls="run-log-content"
          aria-orientation="horizontal"
          aria-valuemin={LOG_PANE_MIN_HEIGHT_PX}
          aria-valuemax={maximumHeight}
          aria-valuenow={paneHeight}
          aria-valuetext={`${paneHeight} pixels high`}
          tabIndex={0}
          onKeyDown={resizeWithKeyboard}
          onPointerDown={(event) => {
            event.preventDefault();
            const maximum = maximumPaneHeight(paneElement.current);
            setMaximumHeight(maximum);
            resizeStart.current = {
              clientY: event.clientY,
              height: boundedPaneHeight(paneHeight, maximum),
            };
            event.currentTarget.setPointerCapture?.(event.pointerId);
          }}
          onPointerMove={(event) => {
            const start = resizeStart.current;
            if (!start) return;
            const maximum = maximumPaneHeight(paneElement.current);
            setMaximumHeight(maximum);
            setPaneHeight(
              boundedPaneHeight(start.height + start.clientY - event.clientY, maximum),
            );
          }}
          onPointerUp={endResize}
          onPointerCancel={endResize}
        />
      )}
      <header className="flex min-w-0 items-center gap-2.5 border-b border-line px-2.5">
        <button
          type="button"
          className="run-log-collapse flex min-w-0 cursor-pointer items-center gap-[7px] border-0 bg-transparent py-1 text-left text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid [&>span:first-child]:w-2.5 [&>span:first-child]:text-acid [&>strong]:text-[10px] [&>strong]:tracking-[.08em] [&>strong]:uppercase"
          aria-expanded={expanded}
          aria-controls="run-log-content"
          onClick={() => setExpanded((value) => !value)}
        >
          <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
          <strong>Logs</strong>
          <span className="run-log-scope truncate font-mono text-[9px] text-secondary">{scopeLabel}</span>
        </button>
        <span className="run-log-count ml-auto whitespace-nowrap font-mono text-[8px] text-muted">
          {combinedLogs.length} {combinedLogs.length === 1 ? "record" : "records"}
        </span>
        {expanded && (
          <button
            type="button"
            className={`toggle run-log-autoscroll inline-flex flex-none cursor-pointer items-center gap-[5px] rounded-full border bg-panel px-2 py-[5px] font-mono text-[8px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid [&>span]:text-xs [&>span]:leading-none [&>span]:text-acid ${following ? "active border-acid text-acid" : "border-line text-secondary"}`}
            aria-label="Auto-scroll logs"
            aria-pressed={following}
            title={following ? "Pause auto-scroll" : "Jump to latest logs and resume auto-scroll"}
            onClick={() => {
              if (following) {
                setFollowing(false);
              } else {
                enableAutoScroll();
              }
            }}
          >
            <span aria-hidden="true">↓</span>
            {following ? "Auto-scroll on" : "Auto-scroll off"}
          </button>
        )}
      </header>
      {expanded && (
        <div className="run-log-content grid min-h-0 min-w-0 grid-rows-[auto_auto_auto_minmax(0,1fr)_auto_auto] px-2.5 pt-[7px] pb-1.5" id="run-log-content">
          {pageError && <p className="inspector-error mb-1.5 rounded-[7px] border border-danger px-2 py-1.5 text-[10px] text-danger [overflow-wrap:anywhere]" role="alert">{pageError}</p>}
          {decodeError && <p className="inspector-error mb-1.5 rounded-[7px] border border-danger px-2 py-1.5 text-[10px] text-danger [overflow-wrap:anywhere]" role="alert">{decodeError}</p>}
          {activePage.nextPageToken && (
            <button
              type="button"
              className="descriptor-page-action run-log-older-action mb-1.5 cursor-pointer justify-self-start rounded-md border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
              disabled={pageLoading}
              aria-busy={pageLoading}
              onClick={loadOlderLogs}
            >
              {pageLoading ? "Loading older logs…" : "Load older logs"}
            </button>
          )}
          <div
            className="run-log-scroll min-h-0 min-w-0 overflow-auto rounded-md border border-line bg-[#fbfcfb] [&>.empty-copy]:m-3 [&>.inspector-loading]:m-3"
            ref={scrollElement}
            onScroll={(event) => {
              const element = event.currentTarget;
              const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
              if (distanceFromBottom > SCROLL_LOAD_THRESHOLD_PX) setFollowing(false);
              if (element.scrollTop <= SCROLL_LOAD_THRESHOLD_PX && activePage.nextPageToken) {
                loadOlderLogs();
              }
            }}
          >
            {pageLoading && !combinedLogs.length ? (
              <p className="inspector-loading text-[11px] text-muted italic" role="status">Loading retained logs…</p>
            ) : !combinedLogs.length ? (
              <p className="empty-copy text-[11px] text-muted">
                {nodeId ? "No retained logs are available for this node." : "No retained logs are available for this run."}
              </p>
            ) : (
              <div className="run-log-virtual relative w-full" style={{ height: virtualizer.getTotalSize() }}>
                {virtualizer.getVirtualItems().map((virtualRow) => {
                  const entry = combinedLogs[virtualRow.index];
                  const body = logBodies.get(entry.bodyToken);
                  const graphNodeId = graphNodeByLogIdentity.get(entry.nodeId)?.nodeId;
                  const nodeLabel = graphNodeId
                    ? nodeDisplayNames[graphNodeId] || entry.nodeId
                    : entry.nodeId;
                  return (
                    <article
                      className="run-log-row absolute top-0 left-0 grid min-h-9 w-full grid-cols-[78px_minmax(120px,180px)_52px_minmax(12rem,1fr)] items-start gap-2 border-b border-[#edf0ee] bg-[#fbfcfb] px-[9px] py-1.5 font-mono text-[9px]/[1.45] text-secondary [&>time]:whitespace-nowrap [&>time]:text-muted [&>pre]:m-0 [&>pre]:min-w-0 [&>pre]:whitespace-pre-wrap [&>pre]:text-[#27332d] [&>pre]:[overflow-wrap:anywhere]"
                      key={entry.sequence}
                      data-index={virtualRow.index}
                      ref={virtualizer.measureElement}
                      style={{ transform: `translateY(${virtualRow.start}px)` }}
                    >
                      <time dateTime={new Date(entry.timestamp * 1000).toISOString()}>
                        {logTimestamp(entry.timestamp)}
                      </time>
                      {graphNodeId ? (
                        <button type="button" className="run-log-node min-w-0 cursor-pointer border-0 bg-transparent p-0 text-left [font:inherit] text-acid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid [&>strong]:block [&>strong]:truncate [&>code]:mt-0.5 [&>code]:block [&>code]:truncate [&>code]:text-[8px] [&>code]:text-muted" onClick={() => onSelectNode(graphNodeId)}>
                          <strong>{nodeLabel}</strong>
                          {nodeLabel !== entry.nodeId && <code>{graphNodeId}</code>}
                        </button>
                      ) : (
                        <span className="run-log-node min-w-0 text-left [font:inherit] text-acid [&>strong]:block [&>strong]:truncate">
                          <strong>{nodeLabel}</strong>
                        </span>
                      )}
                      <span className={`run-log-level font-bold uppercase ${entry.level === "error" || entry.level === "critical" ? `level-${entry.level} text-danger` : entry.level === "warning" ? "level-warning text-amber" : "text-secondary"}`}>{entry.level}</span>
                      <pre>{body ?? (droppedTokens.current.has(entry.bodyToken) ? "[log body omitted]" : "Loading…")}</pre>
                    </article>
                  );
                })}
              </div>
            )}
          </div>
          {decodePending && <p className="inspector-loading run-log-decoding mt-[5px] text-[11px] text-muted italic" role="status">Decoding log text…</p>}
          {!pageLoading && !activePage.nextPageToken && combinedLogs.length > 0 && (
            <p className="inspector-end-state mt-[5px] text-center font-mono text-[8px] text-muted uppercase">Start of retained logs</p>
          )}
        </div>
      )}
    </section>
  );
}
