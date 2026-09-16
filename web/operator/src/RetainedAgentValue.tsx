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
import { isUnknownRecord } from "./guards";
import { DescriptorPageOrder, type AgentEventDescriptorMsg } from "./model";
import { ValueView } from "./ValueView";

interface RetainedAgentValueProps {
  api: OperatorApi;
  kind: "inputs" | "outputs";
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

type ValueState =
  | { api: OperatorApi; token: string; status: "loading" }
  | { api: OperatorApi; token: string; status: "ready"; body: unknown }
  | { api: OperatorApi; token: string; status: "error"; error: string };

// Each mounted direction owns its cursor, cancellation and one bounded cached body.
// The parent keys this view by the historical descriptor scope.
export function RetainedAgentValue({
  api,
  kind,
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
  const order =
    kind === "inputs" ? DescriptorPageOrder.FORWARD : DescriptorPageOrder.NEWEST_FIRST;
  const eventKind = kind === "inputs" ? "run.started" : "run.succeeded";
  const label = kind === "inputs" ? "input" : "output";
  const valueEvent = useMemo(() => {
    const records = new Map<string, AgentEventDescriptorMsg>();
    for (const event of page.records) records.set(event.eventSequence, event);
    for (const event of liveEvents) records.set(event.eventSequence, event);
    return boundDescriptors(
      records,
      (event) => event.eventSequence,
      kind === "inputs" ? "newer" : "older",
      [...records.values()]
        .filter((event) => event.eventKind === eventKind)
        .map((event) => event.eventSequence),
    )
      .reverse()
      .find((event) => event.eventKind === eventKind);
  }, [eventKind, kind, liveEvents, page.records]);

  useEffect(() => {
    const controller = new AbortController();
    pageController.current = controller;
    const generation = ++pageGeneration.current;
    setPageState(undefined);
    setPageError(undefined);
    setLoading(Boolean(eventPageToken));
    if (!eventPageToken) {
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
          order,
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
  }, [api, asOfEventUlid, eventPageToken, nodeId, operatorInstanceId, order, runId]);

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
          afterEventSequence: kind === "inputs" ? page.nextCursor : "0",
          beforeEventSequence: kind === "outputs" ? page.nextCursor : "0",
          pageSize: DESCRIPTOR_PAGE_SIZE,
          order,
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
            kind === "inputs" ? "newer" : "older",
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

  const token = valueEvent?.bodyToken;
  const sizeBytes = valueEvent?.sizeBytes;
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
        setValueState({ api, token, status: "ready", body });
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
  }, [api, sizeBytes, token]);

  const value = valueState?.api === api && valueState.token === token ? valueState : undefined;
  const body =
    value?.status === "ready" && isUnknownRecord(value.body) ? value.body : undefined;
  const payload = body && isUnknownRecord(body.data) ? body.data : body;
  const hasRetainedValue = Boolean(payload && Object.hasOwn(payload, kind));
  const retainedValue = hasRetainedValue ? payload?.[kind] : undefined;
  const retainedFields = isUnknownRecord(retainedValue) ? retainedValue : undefined;
  const waiting =
    !activeError && ((pageState?.api !== api && !valueEvent) || (token && !value));
  const awaitingOutput =
    kind === "outputs" &&
    !page.nextPageToken &&
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
          <div className="mt-2 min-w-0 rounded-md bg-canvas p-2">
            <ValueView value={retainedValue} />
          </div>
        )
      ) : value?.status !== "error" && !activeError ? (
        <p className="text-[11px] text-muted">
          {awaitingOutput
            ? "No output yet. This step has not finished."
            : `No retained ${label} is available${page.nextPageToken ? " in the loaded events" : ""}.`}
        </p>
      ) : null}
      {page.nextPageToken && (
        <button
          type="button"
          className="descriptor-page-action cursor-pointer rounded-md border border-line bg-panel px-2 py-[5px] font-mono text-[8px] text-acid disabled:cursor-wait disabled:text-muted"
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
