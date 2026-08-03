import { describe, expect, it, vi } from "vitest";

import {
  GrpcWebOperatorApi,
  type AgentEventPageRequest,
  type LogPageRequest,
} from "./api";
import type { OperatorServiceClient } from "./generated/operator.client";
import {
  AgentEventDescriptorMsg,
  AgentEventPage,
  CatalogSnapshotMsg,
  DescriptorPageOrder,
  DetailChunk,
  LogPage,
  LogRecordDescriptorMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
  RunSummaryPage,
} from "./generated/operator";

function apiWith(client: object): GrpcWebOperatorApi {
  return new GrpcWebOperatorApi(
    "http://operator.test",
    client as unknown as OperatorServiceClient,
  );
}
function detailStream(parts: Uint8Array[]) {
  return (async function* () {
    for (const data of parts) yield DetailChunk.create({ data });
  })();
}

describe("GrpcWebOperatorApi", () => {
  it("loads a summary-only baseline without requesting run snapshots", async () => {
    const signal = new AbortController().signal;
    const catalog = CatalogSnapshotMsg.create({
      operatorInstanceId: "operator-1",
      asOfSequence: "8",
      revision: "3",
    });
    const summary = RunSummaryMsg.create({ runId: "run-1", revision: "2" });
    const getCatalog = vi.fn(() => ({ response: Promise.resolve(catalog) }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPage.create({
          operatorInstanceId: "operator-1",
          asOfSequence: "8",
          runs: [summary],
        }),
      ),
    }));
    const getRunSnapshot = vi.fn();
    const getLatestRunSnapshot = vi.fn();
    const api = apiWith({
      getCatalog,
      listRunSummaries,
      getRunSnapshot,
      getLatestRunSnapshot,
    });

    await expect(api.loadBaseline(signal)).resolves.toEqual({
      catalog,
      asOfSequence: "8",
      runs: [summary],
    });
    expect(listRunSummaries).toHaveBeenCalledOnce();
    expect(listRunSummaries).toHaveBeenCalledWith(
      { workflowSelector: "", pageSize: 100, pageToken: "" },
      { abort: signal },
    );
    expect(getCatalog).toHaveBeenCalledTimes(2);
    expect(getCatalog).toHaveBeenNthCalledWith(1, {}, { abort: signal });
    expect(getCatalog).toHaveBeenNthCalledWith(2, {}, { abort: signal });
    expect(getRunSnapshot).not.toHaveBeenCalled();
    expect(getLatestRunSnapshot).not.toHaveBeenCalled();
  });

  it("requests exactly one typed log and event page with filters, order, and cancellation", async () => {
    const signal = new AbortController().signal;
    const log = LogRecordDescriptorMsg.create({ sequence: "10", nodeId: "agent" });
    const event = AgentEventDescriptorMsg.create({ eventSequence: "3" });
    const listLogs = vi.fn(() => ({
      response: Promise.resolve(
        LogPage.create({
          operatorInstanceId: "operator-1",
          asOfSequence: "20",
          logs: [log],
          nextPageToken: "log-next",
        }),
      ),
    }));
    const listAgentEvents = vi.fn(() => ({
      response: Promise.resolve(
        AgentEventPage.create({
          operatorInstanceId: "operator-1",
          asOfSequence: "20",
          runId: "run-1",
          nodeId: "agent",
          events: [event],
          nextPageToken: "event-next",
        }),
      ),
    }));
    const api = apiWith({ listLogs, listAgentEvents });
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

    await expect(api.listLogPage(logRequest, signal)).resolves.toEqual({
      operatorInstanceId: "operator-1",
      asOfSequence: "20",
      records: [log],
      nextPageToken: "log-next",
      nextCursor: "10",
    });
    await expect(api.listAgentEventPage(eventRequest, signal)).resolves.toEqual({
      operatorInstanceId: "operator-1",
      asOfSequence: "20",
      runId: "run-1",
      nodeId: "agent",
      records: [event],
      nextPageToken: "event-next",
      nextCursor: "3",
    });
    expect(listLogs).toHaveBeenCalledOnce();
    expect(listLogs).toHaveBeenCalledWith(
      {
        pageToken: "log-page",
        afterSequence: "0",
        beforeSequence: "11",
        pageSize: 25,
        nodeId: "agent",
        order: DescriptorPageOrder.NEWEST_FIRST,
      },
      { abort: signal },
    );
    expect(listAgentEvents).toHaveBeenCalledOnce();
    expect(listAgentEvents).toHaveBeenCalledWith(
      {
        pageToken: "event-page",
        afterEventSequence: "2",
        beforeEventSequence: "0",
        pageSize: 30,
        order: DescriptorPageOrder.FORWARD,
      },
      { abort: signal },
    );
  });

  it("rejects a page that cannot advance its continuation", async () => {
    const api = apiWith({
      listLogs: vi.fn(() => ({
        response: Promise.resolve(
          LogPage.create({
            operatorInstanceId: "operator-1",
            asOfSequence: "20",
            nextPageToken: "same-page",
          }),
        ),
      })),
    });

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
    const snapshot = RunSnapshotMsg.create({ operatorInstanceId: "operator-1" });
    const getLatestRunSnapshot = vi.fn(() => ({ response: Promise.resolve(snapshot) }));
    const responses = detailStream([]);
    const streamOperatorUpdates = vi.fn(() => ({ responses }));
    const api = apiWith({ getLatestRunSnapshot, streamOperatorUpdates });

    await expect(
      api.getLatestRunSnapshot("run-1", "operator-1", signal),
    ).resolves.toBe(snapshot);
    expect(api.streamUpdates("operator-1", "9", signal)).toBe(responses);
    expect(getLatestRunSnapshot).toHaveBeenCalledWith(
      { runId: "run-1", operatorInstanceId: "operator-1" },
      { abort: signal },
    );
    expect(streamOperatorUpdates).toHaveBeenCalledWith(
      { operatorInstanceId: "operator-1", afterSequence: "9" },
      { abort: signal },
    );
  });

  it("decodes UTF-8 across chunks and never JSON-parses plain log text", async () => {
    const signal = new AbortController().signal;
    const encoder = new TextEncoder();
    const json = encoder.encode('{"message":"A😀B"}');
    const emojiStart = json.indexOf(0xf0);
    const text = encoder.encode("plain log: not JSON }");
    const readDetail = vi.fn(({ bodyToken }: { bodyToken: string }) => ({
      responses:
        bodyToken === "json-body"
          ? detailStream([
              json.slice(0, emojiStart + 2),
              json.slice(emojiStart + 2, emojiStart + 3),
              json.slice(emojiStart + 3),
            ])
          : detailStream([text.slice(0, 7), text.slice(7)]),
    }));
    const api = apiWith({ readDetail });

    await expect(api.readJsonDetail("json-body", signal)).resolves.toEqual({
      message: "A😀B",
    });
    await expect(api.readTextDetail("text-body", signal)).resolves.toBe(
      "plain log: not JSON }",
    );
    expect(readDetail).toHaveBeenNthCalledWith(
      1,
      { bodyToken: "json-body" },
      { abort: signal },
    );
    expect(readDetail).toHaveBeenNthCalledWith(
      2,
      { bodyToken: "text-body" },
      { abort: signal },
    );
  });
});
