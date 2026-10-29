import { useEffect, useMemo, useRef, useState } from "react";

import type { AgentFieldSchemas } from "./GraphCanvas";
import { InspectorFields } from "./InspectorDefinition";
import type { OperatorApi } from "./api";
import {
  boundDescriptors,
  DESCRIPTOR_PAGE_SIZE,
  DETAIL_CACHE_MAX_BYTES,
  type DescriptorPageState,
  measuredByteCost,
  mergeDescriptorPage,
} from "./detailProjection";
import { parseAgentLiveEvent, parseAgentTraceTerminal } from "./agentTrace/common";
import { isUnknownRecord } from "./guards";
import {
  DescriptorPageOrder,
  type AgentEventDescriptorMsg,
  type TraceDescriptorMsg,
} from "./model";
import { ValueView } from "./ValueView";

interface RetainedAgentValueProps {
  api: OperatorApi;
  kind: "inputs" | "outputs";
  /** The step's final event, which carries its outputs once it finished. */
  trace?: TraceDescriptorMsg;
  fields: AgentFieldSchemas["inputs"] | undefined;
  schemaUnavailableMessage: string;
  operatorInstanceId: string;
  asOfEventUlid: string;
  runId: string;
  nodeId: string;
  eventPageToken: string;
  liveEvents: AgentEventDescriptorMsg[];
  nodeStatus?: string;
}

const EMPTY_PAGE: DescriptorPageState<AgentEventDescriptorMsg> = {
  records: [],
  nextPageToken: "",
  nextCursor: "0",
};

interface Retained {
  /** Absent when the body holds no value, such as a run that returned no outputs. */
  value: unknown;
  /** Fields the trace left out because they were too large to keep. */
  omitted: string[];
}

type ValueState =
  | { api: OperatorApi; token: string; status: "loading" }
  | { api: OperatorApi; token: string; status: "ready"; retained: Retained }
  | { api: OperatorApi; token: string; status: "error"; error: string };

// The run's inputs travel on its common run.started event; its outputs on its final event.
function retainedValue(body: unknown, kind: "inputs" | "outputs"): Retained {
  if (kind === "inputs") {
    const { common } = parseAgentLiveEvent(body);
    return common?.kind === "run.started"
      ? { value: common.inputs, omitted: common.omitted_inputs }
      : { value: undefined, omitted: [] };
  }
  const terminal = parseAgentTraceTerminal(body);
  return terminal.kind === "trace_finished" && terminal.outputs !== null
    ? { value: terminal.outputs, omitted: terminal.omitted_outputs }
    : { value: undefined, omitted: [] };
}

// Inputs page through the step's events for its run.started event; outputs read the
// final event. Each mounted direction owns its cursor, cancellation and one bounded
// cached body. The parent keys this view by the historical descriptor scope.
export function RetainedAgentValue({
  api,
  kind,
  trace,
  fields,
  operatorInstanceId,
  asOfEventUlid,
  runId,
  nodeId,
  eventPageToken,
  liveEvents,
  nodeStatus,
  schemaUnavailableMessage,
}: RetainedAgentValueProps) {
  const [pageState, setPageState] = useState<{
    api: OperatorApi;
    page: DescriptorPageState<AgentEventDescriptorMsg>;
  }>();
  const [pageError, setPageError] = useState<{ api: OperatorApi; error: string }>();
  const [loading, setLoading] = useState(false);
  const [valueState, setValueState] = useState<ValueState>();
  const pageController = useRef<AbortController | null>(null);
  const pageGeneration = useRef(0);
  const page = pageState?.api === api ? pageState.page : EMPTY_PAGE;
  const activeError = pageError?.api === api ? pageError.error : undefined;
  const pagesEvents = kind === "inputs";
  const label = kind === "inputs" ? "input" : "output";
  const valueEvent = useMemo(() => {
    if (!pagesEvents) return undefined;
    const records = new Map<string, AgentEventDescriptorMsg>();
    for (const event of page.records) records.set(event.eventSequence, event);
    for (const event of liveEvents) records.set(event.eventSequence, event);
    const isRunStarted = (event: AgentEventDescriptorMsg) =>
      event.agentEventKind === "run.started";
    return boundDescriptors(
      records,
      (event) => event.eventSequence,
      "newer",
      [...records.values()].filter(isRunStarted).map((event) => event.eventSequence),
    )
      .reverse()
      .find(isRunStarted);
  }, [liveEvents, page.records, pagesEvents]);

  useEffect(() => {
    const controller = new AbortController();
    pageController.current = controller;
    const generation = ++pageGeneration.current;
    setPageState(undefined);
    setPageError(undefined);
    setLoading(pagesEvents && Boolean(eventPageToken));
    if (!pagesEvents || !eventPageToken) {
      setPageState({ api, page: EMPTY_PAGE });
      return () => controller.abort();
    }
    void api
      .listAgentEventPage(
        {
          pageToken: eventPageToken,
          afterEventSequence: "0",
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
      .then((next) => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setPageState({ api, page: next });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setPageError({
          api,
          error: error instanceof Error ? error.message : "Events unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setLoading(false);
        pageController.current = null;
      });
    return () => {
      controller.abort();
      pageController.current?.abort();
      pageController.current = null;
    };
  }, [api, asOfEventUlid, eventPageToken, nodeId, operatorInstanceId, pagesEvents, runId]);

  function loadMore() {
    if (!page.nextPageToken || pageController.current) return;
    const controller = new AbortController();
    pageController.current = controller;
    const generation = ++pageGeneration.current;
    setPageError(undefined);
    setLoading(true);
    void api
      .listAgentEventPage(
        {
          pageToken: page.nextPageToken,
          afterEventSequence: page.nextCursor,
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
      .then((next) => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setPageState((current) => ({
          api,
          page: mergeDescriptorPage(
            current?.api === api ? current.page : EMPTY_PAGE,
            next,
            (event) => event.eventSequence,
            "newer",
            valueEvent ? [valueEvent.eventSequence] : [],
          ),
        }));
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setPageError({
          api,
          error: error instanceof Error ? error.message : "Events unavailable",
        });
      })
      .finally(() => {
        if (controller.signal.aborted || generation !== pageGeneration.current) return;
        setLoading(false);
        pageController.current = null;
      });
  }

  const token = pagesEvents ? valueEvent?.bodyToken : trace?.bodyToken;
  const sizeBytes = pagesEvents ? valueEvent?.sizeBytes : trace?.sizeBytes;
  useEffect(() => {
    if (!token) {
      setValueState(undefined);
      return;
    }
    const controller = new AbortController();
    setValueState({ api, token, status: "loading" });
    void api
      .readJsonDetail(token, controller.signal)
      .then((body) => {
        if (controller.signal.aborted) return;
        if (measuredByteCost(body, sizeBytes) > DETAIL_CACHE_MAX_BYTES) {
          setValueState({
            api,
            token,
            status: "error",
            error: "Retained value exceeds the browser detail limit.",
          });
          return;
        }
        setValueState({ api, token, status: "ready", retained: retainedValue(body, kind) });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setValueState({
          api,
          token,
          status: "error",
          error: error instanceof Error ? error.message : "Detail unavailable",
        });
      });
    return () => controller.abort();
  }, [api, kind, sizeBytes, token]);

  const value = valueState?.api === api && valueState.token === token ? valueState : undefined;
  const retained = value?.status === "ready" ? value.retained.value : undefined;
  const omitted = value?.status === "ready" ? value.retained.omitted : [];
  const hasRetainedValue = retained !== undefined;
  const retainedFields = isUnknownRecord(retained) ? retained : undefined;
  const waiting =
    !activeError &&
    ((pagesEvents && pageState?.api !== api && !valueEvent) || (token && !value));
  const awaitingOutput =
    kind === "outputs" &&
    !token &&
    (!nodeStatus || nodeStatus === "pending" || nodeStatus === "running");

  return (
    <div className="retained-agent-value min-w-0">
      <InspectorFields
        fields={fields}
        values={retainedFields}
        unavailableMessage={schemaUnavailableMessage}
      />
      {activeError && (
        <p className="text-[10px] text-danger [overflow-wrap:anywhere]" role="alert">
          {activeError}
        </p>
      )}
      {value?.status === "error" && (
        <p className="text-[10px] text-danger [overflow-wrap:anywhere]" role="alert">
          {value.error}
        </p>
      )}
      {waiting || value?.status === "loading" ? (
        <p className="text-[11px] text-muted italic" role="status">
          Loading retained {label}…
        </p>
      ) : hasRetainedValue ? (
        retainedFields === undefined && (
          <div className="mt-2 min-w-0 rounded bg-canvas p-2">
            <ValueView value={retained} />
          </div>
        )
      ) : value?.status !== "error" && !activeError ? (
        <p className="text-[11px] text-muted">
          {awaitingOutput
            ? "No output yet. This step has not finished."
            : `No retained ${label} is available${page.nextPageToken ? " in the loaded events" : ""}.`}
        </p>
      ) : null}
      {omitted.length > 0 && (
        <p className="text-[11px] text-muted [overflow-wrap:anywhere]" role="note">
          Too large to keep in the trace: {omitted.join(", ")}.
        </p>
      )}
      {pagesEvents && page.nextPageToken && (
        <button
          type="button"
          className="descriptor-page-action cursor-pointer rounded border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
          disabled={loading}
          aria-busy={loading}
          onClick={loadMore}
        >
          {loading ? `Loading ${label} events…` : `Load more ${label} events`}
        </button>
      )}
    </div>
  );
}
