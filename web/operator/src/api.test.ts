import { describe, expect, it, vi } from "vitest";

import { GrpcWebOperatorApi, type AgentEventPageRequest, type LogPageRequest } from "./api";
import {
  ActivityDetailChunkV2,
  ActivityDetailRefV2,
  ContinuationRefV2,
  FlowListV2,
  LifecycleCursorV2,
  NodeSnapshotV2,
  PageOrderV2,
  RunActivityDescriptorV2,
  RunActivityPageV2,
  RunSnapshotV2,
  RunSummaryPageV2,
  RunSummaryV2,
  ScopeReferenceV2,
} from "./generated/operator";
import type { IOperatorServiceV2Client } from "./generated/operator.client";
import {
  AgentEventDescriptorMsg,
  DescriptorPageOrder,
  LogRecordDescriptorMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
} from "./model";

type ClientOverrides = Partial<Record<keyof IOperatorServiceV2Client, unknown>>;

function apiWith(client: ClientOverrides): GrpcWebOperatorApi {
  return new GrpcWebOperatorApi(
    "http://operator.test",
    client as IOperatorServiceV2Client,
  );
}

function scope(reference = "operator-1") {
  return ScopeReferenceV2.create({ reference });
}

function cursor(sourceSequence: string, stream = "run-summaries") {
  return LifecycleCursorV2.create({
    stream,
    streamGeneration: "1",
    sourceSequence,
  });
}

function continuation(
  continuationId: string,
  sourceSequence = "20",
  stream = "activity:run-1:logs",
) {
  return ContinuationRefV2.create({
    scopeRef: scope(),
    continuationId,
    cursor: cursor(sourceSequence, stream),
  });
}

function detailRef(objectKey: string) {
  return ActivityDetailRefV2.create({
    runId: "run-1",
    scopeRef: scope(),
    activityId: objectKey,
    runSequence: "1",
    objectUri: `local://detail/${objectKey}`,
    objectKey,
  });
}

function detailStream(parts: Uint8Array[]) {
  return (async function* () {
    for (const data of parts) yield ActivityDetailChunkV2.create({ data });
  })();
}

describe("GrpcWebOperatorApi", () => {
  it("loads a summary-only baseline without requesting run snapshots", async () => {
    const signal = new AbortController().signal;
    const summary = RunSummaryMsg.create({ runId: "run-1", revision: "2" });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: cursor("8", "flows"), scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPageV2.create({
          cursor: cursor("8"),
          scopeRef: scope(),
          runs: [RunSummaryV2.create({ runId: summary.runId, revision: summary.revision })],
        }),
      ),
    }));
    const getRunSnapshot = vi.fn();
    const api = apiWith({ discoverFlows, listRunSummaries, getRunSnapshot });

    const baseline = await api.loadBaseline(signal);

    expect(baseline.catalog).toMatchObject({
      operatorInstanceId: "operator-1",
      asOfSequence: "8",
      workflows: [],
      scanTargets: [],
      diagnostics: [],
    });
    expect(baseline.runs).toEqual([summary]);
    expect(listRunSummaries).toHaveBeenCalledOnce();
    expect(listRunSummaries).toHaveBeenCalledWith(
      { workflowSelector: "", pageSize: 100 },
      { abort: signal },
    );
    expect(discoverFlows).toHaveBeenCalledTimes(2);
    expect(discoverFlows).toHaveBeenNthCalledWith(1, { pageSize: 200 }, { abort: signal });
    expect(discoverFlows).toHaveBeenNthCalledWith(2, { pageSize: 200 }, { abort: signal });
    expect(getRunSnapshot).not.toHaveBeenCalled();
  });

  it("accepts a stable catalog when non-catalog updates advance the run baseline", async () => {
    const summary = RunSummaryMsg.create({ runId: "run-1", revision: "2" });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: cursor("0", "flows"), scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPageV2.create({
          cursor: cursor("2"),
          scopeRef: scope(),
          runs: [RunSummaryV2.create({ runId: summary.runId, revision: summary.revision })],
        }),
      ),
    }));
    const api = apiWith({ discoverFlows, listRunSummaries });

    const baseline = await api.loadBaseline();

    expect(baseline.catalog).toMatchObject({
      operatorInstanceId: "operator-1",
      asOfSequence: "0",
    });
    expect(baseline.asOfSequence).toBe("2");
    expect(baseline.runs).toEqual([summary]);
  });

  it("requests exactly one typed activity page for logs and events", async () => {
    const signal = new AbortController().signal;
    const log = LogRecordDescriptorMsg.create({ sequence: "10", nodeId: "agent", bodyToken: "log-body" });
    const event = AgentEventDescriptorMsg.create({ eventSequence: "3", bodyToken: "event-body" });
    const logPage = continuation("log-page", "20", "activity:run-1:logs");
    const eventPage = continuation("event-page", "20", "activity:run-1:agent");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor("20", "run:run-1"),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1" }),
          logContinuation: logPage,
          nodes: [NodeSnapshotV2.create({ nodeId: "agent", activityContinuation: eventPage })],
        }),
      ),
    }));
    const listRunActivity = vi.fn((request: { nodeId: string }) => ({
      response: Promise.resolve(
        RunActivityPageV2.create({
          cursor: cursor("20", request.nodeId ? "activity:run-1:agent" : "activity:run-1:logs"),
          runId: "run-1",
          scopeRef: scope(),
          activities: [
            request.nodeId
              ? RunActivityDescriptorV2.create({
                  runSequence: "3",
                  kind: "agent_event",
                  nodeId: "agent",
                  detailRef: detailRef("event-body"),
                })
              : RunActivityDescriptorV2.create({
                  runSequence: "10",
                  kind: "log",
                  nodeId: "agent",
                  detailRef: detailRef("log-body"),
                }),
          ],
          nextPage: continuation(
            request.nodeId ? "event-next" : "log-next",
            "20",
            request.nodeId ? "activity:run-1:agent" : "activity:run-1:logs",
          ),
        }),
      ),
    }));
    const api = apiWith({ getRunSnapshot, listRunActivity });
    await api.getLatestRunSnapshot("run-1", "operator-1");

    const logRequest: LogPageRequest = {
      pageToken: "log-page",
      afterSequence: "0",
      beforeSequence: "11",
      pageSize: 25,
      nodeId: "agent",
      order: DescriptorPageOrder.NEWEST_FIRST,
      expectedOperatorInstanceId: "operator-1",
      expectedAsOfSequence: "20",
    };
    const eventRequest: AgentEventPageRequest = {
      pageToken: "event-page",
      afterEventSequence: "2",
      beforeEventSequence: "0",
      pageSize: 30,
      order: DescriptorPageOrder.FORWARD,
      expectedOperatorInstanceId: "operator-1",
      expectedAsOfSequence: "20",
      expectedRunId: "run-1",
      expectedNodeId: "agent",
    };

    await expect(api.listLogPage(logRequest, signal)).resolves.toMatchObject({
      operatorInstanceId: "operator-1",
      asOfSequence: "20",
      records: [log],
      nextPageToken: "log-next",
      nextCursor: "10",
    });
    await expect(api.listAgentEventPage(eventRequest, signal)).resolves.toMatchObject({
      operatorInstanceId: "operator-1",
      asOfSequence: "20",
      runId: "run-1",
      nodeId: "agent",
      records: [
        expect.objectContaining({
          eventSequence: event.eventSequence,
          sizeBytes: event.sizeBytes,
          bodyToken: event.bodyToken,
          invocationId: event.invocationId,
          eventKind: event.eventKind,
          error: event.error,
          toolCount: event.toolCount,
          predictCount: event.predictCount,
        }),
      ],
      nextPageToken: "event-next",
      nextCursor: "3",
    });
    expect(listRunActivity).toHaveBeenCalledTimes(2);
    expect(listRunActivity).toHaveBeenNthCalledWith(
      1,
      {
        runId: "run-1",
        pageSize: 25,
        continuation: logPage,
        nodeId: "",
        order: PageOrderV2.NEWEST_FIRST,
      },
      { abort: signal },
    );
    expect(listRunActivity).toHaveBeenNthCalledWith(
      2,
      {
        runId: "run-1",
        pageSize: 30,
        continuation: eventPage,
        nodeId: "agent",
        order: PageOrderV2.FORWARD,
      },
      { abort: signal },
    );
  });

  it("rejects a page that cannot advance its continuation", async () => {
    const samePage = continuation("same-page", "20", "activity:run-1:logs");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor("20", "run:run-1"),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1" }),
          logContinuation: samePage,
        }),
      ),
    }));
    const listRunActivity = vi.fn(() => ({
      response: Promise.resolve(
        RunActivityPageV2.create({
          cursor: cursor("20", "activity:run-1:logs"),
          runId: "run-1",
          scopeRef: scope(),
          nextPage: samePage,
        }),
      ),
    }));
    const api = apiWith({ getRunSnapshot, listRunActivity });
    await api.getLatestRunSnapshot("run-1", "operator-1");

    await expect(
      api.listLogPage({
        pageToken: "same-page",
        afterSequence: "4",
        beforeSequence: "0",
        pageSize: 25,
        nodeId: "",
        order: DescriptorPageOrder.FORWARD,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfSequence: "20",
      }),
    ).rejects.toThrow("Log pagination made no progress");
  });

  it("propagates cancellation to latest snapshots and update streams", async () => {
    const signal = new AbortController().signal;
    const snapshot = RunSnapshotV2.create({ scopeRef: scope() });
    const getRunSnapshot = vi.fn(() => ({ response: Promise.resolve(snapshot) }));
    const responses = (async function* () {})();
    const watchRunStatus = vi.fn(() => ({ responses }));
    const api = apiWith({ getRunSnapshot, watchRunStatus });

    await expect(api.getLatestRunSnapshot("run-1", "operator-1", signal)).resolves.toEqual(
      RunSnapshotMsg.create({ operatorInstanceId: "operator-1" }),
    );
    const updates = [];
    for await (const update of api.streamUpdates("operator-1", "9", signal)) {
      updates.push(update);
    }

    expect(updates).toEqual([]);
    expect(getRunSnapshot).toHaveBeenCalledWith({ runId: "run-1" }, { abort: signal });
    expect(watchRunStatus).toHaveBeenCalledWith(
      { afterCursor: LifecycleCursorV2.create({ sourceSequence: "9" }) },
      { abort: signal },
    );
  });

  it("decodes UTF-8 across chunks and never JSON-parses plain log text", async () => {
    const signal = new AbortController().signal;
    const encoder = new TextEncoder();
    const json = encoder.encode('{"message":"A😀B"}');
    const emojiStart = json.indexOf(0xf0);
    const text = encoder.encode("plain log: not JSON }");
    const readActivityDetail = vi.fn(
      ({ detailRef: reference }: { detailRef?: ActivityDetailRefV2 }) => ({
        responses:
          reference?.objectKey === "json-body"
            ? detailStream([
                json.slice(0, emojiStart + 2),
                json.slice(emojiStart + 2, emojiStart + 3),
                json.slice(emojiStart + 3),
              ])
            : detailStream([text.slice(0, 7), text.slice(7)]),
      }),
    );
    const api = apiWith({ readActivityDetail });

    await expect(api.readJsonDetail("json-body", signal)).resolves.toEqual({
      message: "A😀B",
    });
    await expect(api.readTextDetail("text-body", signal)).resolves.toBe(
      "plain log: not JSON }",
    );
    expect(readActivityDetail).toHaveBeenCalledTimes(2);
    expect(readActivityDetail.mock.calls[0][0].detailRef).toMatchObject({
      objectUri: "local://detail/json-body",
      objectKey: "json-body",
    });
    expect(readActivityDetail.mock.calls[1][0].detailRef).toMatchObject({
      objectUri: "local://detail/text-body",
      objectKey: "text-body",
    });
    expect(readActivityDetail).toHaveBeenNthCalledWith(
      1,
      expect.anything(),
      { abort: signal },
    );
    expect(readActivityDetail).toHaveBeenNthCalledWith(
      2,
      expect.anything(),
      { abort: signal },
    );
  });
});
