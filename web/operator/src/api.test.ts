import { describe, expect, it, vi } from "vitest";

import { GrpcWebOperatorApi, type AgentEventPageRequest, type LogPageRequest } from "./api";
import {
  ActivityDetailChunkV2,
  ActivityDetailRefV2,
  CatalogReloadRequiredV2,
  ContinuationRefV2,
  FlowListV2,
  LifecycleCursorV2,
  NodeSnapshotV2,
  PageOrderV2,
  ProjectSummaryCursorV2,
  RunActivityDescriptorV2,
  RunActivityPageV2,
  RunSnapshotV2,
  RunStatusEnvelopeV2,
  RunSummaryPageV2,
  RunSummaryV2,
  ScopeReferenceV2,
  WorkflowNodeSourceV2,
  TerminalSealV2,
  TraceDescriptorV2,
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
  return new GrpcWebOperatorApi("http://operator.test", client as IOperatorServiceV2Client);
}

function scope(reference = "operator-1") {
  return ScopeReferenceV2.create({ reference });
}

function eventUlid(sequence: number): string {
  return sequence.toString(16).toUpperCase().padStart(26, "0");
}

function cursor(sequence: number, _stream = "operator-events") {
  const stream = "operator-events";
  return LifecycleCursorV2.create({
    stream,
    topologyFingerprint: `${stream}-topology`,
    streamGeneration: "1",
    retainedFloorEventUlid: eventUlid(Math.min(sequence, 1)),
    eventUlid: eventUlid(sequence),
  });
}

function continuation(continuationId: string, sequence = 20, stream = "activity:run-1:logs") {
  return ContinuationRefV2.create({
    scopeRef: scope(),
    continuationId,
    cursor: cursor(sequence, stream),
  });
}

function projectSummaryCursor(
  values: Partial<ProjectSummaryCursorV2> = {},
): ProjectSummaryCursorV2 {
  return ProjectSummaryCursorV2.create({
    stream: "project-summaries",
    topologyFingerprint: "summary-topology",
    sourceGeneration: "2026-08-19T14:57:00Z",
    retainedFloorSequence: "9007199254740993",
    targetHeadSequence: "9007199254740995",
    checkpointWatermark: "9007199254740997",
    checkpointDigest: "checkpoint-digest-1",
    ...values,
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
    sha256: "a".repeat(64),
    sizeBytes: "0",
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
    const summary = RunSummaryMsg.create({
      runId: "run-1",
      flowName: "flows.py::orders",
      workflowId: "flows.py::orders",
      revision: "2",
    });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: cursor(8, "flows"), scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPageV2.create({
          cursor: cursor(8),
          scopeRef: scope(),
          runs: [
            RunSummaryV2.create({
              runId: summary.runId,
              workflowSelector: summary.workflowId,
              revision: summary.revision,
            }),
          ],
        }),
      ),
    }));
    const getRunSnapshot = vi.fn();
    const api = apiWith({ discoverFlows, listRunSummaries, getRunSnapshot });

    const baseline = await api.loadBaseline(signal);

    expect(baseline.catalog).toMatchObject({
      operatorInstanceId: "operator-1",
      asOfEventUlid: eventUlid(8),
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

  it("requests source for only the selected current node", async () => {
    const signal = new AbortController().signal;
    const getWorkflowNodeSource = vi.fn(() => ({
      response: Promise.resolve(
        WorkflowNodeSourceV2.create({ sourceCode: "@step\ndef normalize():\n    return None" }),
      ),
    }));
    const api = apiWith({ getWorkflowNodeSource });

    await expect(
      api.getWorkflowNodeSource("flows.py::build", "normalize_1", signal),
    ).resolves.toBe("@step\ndef normalize():\n    return None");
    expect(getWorkflowNodeSource).toHaveBeenCalledWith(
      { workflowSelector: "flows.py::build", nodeId: "normalize_1" },
      { abort: signal },
    );
  });

  it("rejects catalog pages that change scope, cursor, or revision", async () => {
    const changedPages = [
      FlowListV2.create({ cursor: cursor(8), scopeRef: scope("operator-2"), revision: "4" }),
      FlowListV2.create({ cursor: cursor(9), scopeRef: scope(), revision: "4" }),
      FlowListV2.create({ cursor: cursor(8), scopeRef: scope(), revision: "5" }),
    ];

    for (const changedPage of changedPages) {
      const firstPage = FlowListV2.create({
        cursor: cursor(8),
        scopeRef: scope(),
        revision: "4",
        nextPage: ContinuationRefV2.create({
          scopeRef: scope(),
          continuationId: "flows:1",
          cursor: cursor(8),
        }),
      });
      const discoverFlows = vi
        .fn()
        .mockReturnValueOnce({ response: Promise.resolve(firstPage) })
        .mockReturnValueOnce({ response: Promise.resolve(changedPage) });
      const api = apiWith({ discoverFlows });

      await expect(api.getCatalog()).rejects.toThrow(
        "Flow catalog changed while loading pages",
      );
      expect(discoverFlows).toHaveBeenCalledTimes(2);
    }
  });

  it("forwards a stable summary cursor chain without converting its opaque source epoch", async () => {
    const lifecycleCursor = cursor(8);
    const summaryCursor = projectSummaryCursor();
    const nextPage = ContinuationRefV2.create({
      scopeRef: scope(),
      continuationId: "page-2",
      cursor: lifecycleCursor,
      projectSummaryCursor: summaryCursor,
    });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: lifecycleCursor, scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(
      (request: {
        workflowSelector: string;
        pageSize: number;
        continuation?: ContinuationRefV2;
      }) => {
        if (!request.continuation) {
          return {
            response: Promise.resolve(
              RunSummaryPageV2.create({
                cursor: lifecycleCursor,
                scopeRef: scope(),
                runs: [RunSummaryV2.create({ runId: "run-1", createdSequence: "1" })],
                nextPage,
                projectSummaryCursor: summaryCursor,
              }),
            ),
          };
        }
        expect(request.continuation).toEqual(nextPage);
        return {
          response: Promise.resolve(
            RunSummaryPageV2.create({
              cursor: lifecycleCursor,
              scopeRef: scope(),
              runs: [RunSummaryV2.create({ runId: "run-2", createdSequence: "2" })],
              projectSummaryCursor: summaryCursor,
            }),
          ),
        };
      },
    );
    const watchRunStatus = vi.fn(() => ({
      responses: (async function* () {})(),
    }));
    const api = apiWith({ discoverFlows, listRunSummaries, watchRunStatus });

    await expect(api.loadBaseline()).resolves.toMatchObject({
      asOfEventUlid: eventUlid(8),
      runs: [{ runId: "run-1" }, { runId: "run-2" }],
    });
    await api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next();

    expect(listRunSummaries).toHaveBeenNthCalledWith(
      1,
      { workflowSelector: "", pageSize: 100 },
      undefined,
    );
    expect(listRunSummaries).toHaveBeenNthCalledWith(
      2,
      { workflowSelector: "", pageSize: 100, continuation: nextPage },
      undefined,
    );
    expect(listRunSummaries.mock.calls[1][0].continuation?.projectSummaryCursor).toMatchObject({
      sourceGeneration: "2026-08-19T14:57:00Z",
      retainedFloorSequence: "9007199254740993",
      targetHeadSequence: "9007199254740995",
      checkpointWatermark: "9007199254740997",
    });
    expect(watchRunStatus).toHaveBeenCalledWith(
      { afterCursor: lifecycleCursor, scopeRef: { reference: "operator-1" } },
      undefined,
    );
  });

  it("accepts an all-absent summary cursor chain for local interoperability", async () => {
    const lifecycleCursor = cursor(8);
    const nextPage = ContinuationRefV2.create({
      scopeRef: scope(),
      continuationId: "page-2",
      cursor: lifecycleCursor,
    });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: lifecycleCursor, scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(
      (request: {
        continuation?: ContinuationRefV2;
        workflowSelector: string;
        pageSize: number;
      }) => ({
        response: Promise.resolve(
          request.continuation
            ? RunSummaryPageV2.create({
                cursor: lifecycleCursor,
                scopeRef: scope(),
                runs: [RunSummaryV2.create({ runId: "run-2", createdSequence: "2" })],
              })
            : RunSummaryPageV2.create({
                cursor: lifecycleCursor,
                scopeRef: scope(),
                runs: [RunSummaryV2.create({ runId: "run-1", createdSequence: "1" })],
                nextPage,
              }),
        ),
      }),
    );
    const api = apiWith({ discoverFlows, listRunSummaries });

    await expect(api.loadBaseline()).resolves.toMatchObject({
      runs: [{ runId: "run-1" }, { runId: "run-2" }],
    });
    expect(listRunSummaries).toHaveBeenCalledTimes(2);
  });

  it("rejects a summary cursor missing from a nonempty continuation", async () => {
    const lifecycleCursor = cursor(8);
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: lifecycleCursor, scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPageV2.create({
          cursor: lifecycleCursor,
          scopeRef: scope(),
          nextPage: ContinuationRefV2.create({
            scopeRef: scope(),
            continuationId: "page-2",
            cursor: lifecycleCursor,
          }),
          projectSummaryCursor: projectSummaryCursor(),
        }),
      ),
    }));
    const api = apiWith({ discoverFlows, listRunSummaries });

    await expect(api.loadBaseline()).rejects.toThrow(
      "Project summary continuation cursor does not match its page",
    );
    expect(listRunSummaries).toHaveBeenCalledOnce();
  });

  it("rejects a changed summary cursor on a later page", async () => {
    const lifecycleCursor = cursor(8);
    const summaryCursor = projectSummaryCursor();
    const nextPage = ContinuationRefV2.create({
      scopeRef: scope(),
      continuationId: "page-2",
      cursor: lifecycleCursor,
      projectSummaryCursor: summaryCursor,
    });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: lifecycleCursor, scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(
      (request: {
        continuation?: ContinuationRefV2;
        workflowSelector: string;
        pageSize: number;
      }) => ({
        response: Promise.resolve(
          request.continuation
            ? RunSummaryPageV2.create({
                cursor: lifecycleCursor,
                scopeRef: scope(),
                projectSummaryCursor: projectSummaryCursor({
                  checkpointDigest: "other-digest",
                }),
              })
            : RunSummaryPageV2.create({
                cursor: lifecycleCursor,
                scopeRef: scope(),
                nextPage,
                projectSummaryCursor: summaryCursor,
              }),
        ),
      }),
    );
    const api = apiWith({ discoverFlows, listRunSummaries });

    await expect(api.loadBaseline()).rejects.toThrow(
      "Project summary cursor changed across run summary pages",
    );
    expect(listRunSummaries).toHaveBeenCalledTimes(2);
  });

  it("preserves a V2 workflow selector on a live run-created summary", async () => {
    const envelope = RunStatusEnvelopeV2.create({
      eventUlid: eventUlid(9),
      cursor: cursor(9, "operator-events"),
      scopeRef: scope(),
      payload: {
        oneofKind: "runCreated",
        runCreated: {
          summary: RunSummaryV2.create({
            runId: "run-live",
            workflowSelector: "flows.py::orders",
          }),
          nodes: [],
        },
      },
    });
    const watchRunStatus = vi.fn(() => ({
      responses: (async function* () {
        yield envelope;
      })(),
    }));
    const api = apiWith({ watchRunStatus });

    const updates = [];
    for await (const update of api.streamUpdates("operator-1", eventUlid(0))) {
      updates.push(update);
    }

    expect(updates).toMatchObject([
      {
        payload: {
          update: {
            change: {
              runCreated: {
                summary: { workflowId: "flows.py::orders" },
              },
            },
          },
        },
      },
    ]);
    expect(watchRunStatus).toHaveBeenCalledWith(
      { scopeRef: { reference: "operator-1" } },
      undefined,
    );
  });

  it("maps a catalog reload notice and rejects malformed or unknown payloads", async () => {
    const valid = RunStatusEnvelopeV2.create({
      eventUlid: eventUlid(9),
      cursor: cursor(9),
      scopeRef: scope(),
      payload: {
        oneofKind: "catalogReloadRequired",
        catalogReloadRequired: CatalogReloadRequiredV2.create({ deploymentId: "deployment-a" }),
      },
    });
    const validApi = apiWith({
      watchRunStatus: vi.fn(() => ({
        responses: (async function* () {
          yield valid;
        })(),
      })),
    });

    const updates = [];
    for await (const update of validApi.streamUpdates("operator-1", eventUlid(8))) {
      updates.push(update);
    }
    expect(updates).toMatchObject([
      {
        payload: {
          update: {
            change: {
              oneofKind: "catalogReloadRequired",
              catalogReloadRequired: { deploymentId: "deployment-a" },
            },
          },
        },
      },
    ]);

    const malformedPayloads: RunStatusEnvelopeV2["payload"][] = [
      {
        oneofKind: "catalogReloadRequired",
        catalogReloadRequired: CatalogReloadRequiredV2.create(),
      },
      { oneofKind: undefined },
    ];
    for (const payload of malformedPayloads) {
      const api = apiWith({
        watchRunStatus: vi.fn(() => ({
          responses: (async function* () {
            yield RunStatusEnvelopeV2.create({
              eventUlid: eventUlid(9),
              cursor: cursor(9),
              scopeRef: scope(),
              payload,
            });
          })(),
        })),
      });
      await expect(
        api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next(),
      ).rejects.toThrow(/deployment ID|Unknown run status payload/);
    }
  });

  it("maps terminal seals in snapshots and structural activity updates", async () => {
    const activity = RunActivityDescriptorV2.create({
      activityId: "terminal-seal-1",
      runSequence: "4",
      kind: "terminal_seal",
      timestamp: 12,
      terminalSeal: TerminalSealV2.create({
        terminalStatus: "failed",
        reason: "execution failed",
      }),
    });
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor(12),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1", status: "failed" }),
          terminalSeal: activity,
        }),
      ),
    }));
    const watchRunStatus = vi.fn(() => ({
      responses: (async function* () {
        yield RunStatusEnvelopeV2.create({
          eventUlid: eventUlid(13),
          cursor: cursor(13),
          scopeRef: scope(),
          payload: {
            oneofKind: "activityAppended",
            activityAppended: { runId: "run-1", activity },
          },
        });
      })(),
    }));
    const api = apiWith({ getRunSnapshot, watchRunStatus });

    await expect(api.getLatestRunSnapshot("run-1", "operator-1")).resolves.toMatchObject({
      terminalSeal: {
        activityId: "terminal-seal-1",
        runSequence: "4",
        timestamp: 12,
        terminalStatus: "failed",
        reason: "execution failed",
      },
    });

    const updates = [];
    for await (const update of api.streamUpdates("operator-1", eventUlid(12))) {
      updates.push(update);
    }
    expect(updates).toMatchObject([
      {
        payload: {
          update: {
            change: {
              terminalSealAppended: {
                runId: "run-1",
                terminalSeal: {
                  activityId: "terminal-seal-1",
                  terminalStatus: "failed",
                  reason: "execution failed",
                },
              },
            },
          },
        },
      },
    ]);
  });

  it("rejects missing and unknown activity kinds in the stream", async () => {
    const envelopes = [
      RunStatusEnvelopeV2.create({
        eventUlid: eventUlid(2),
        cursor: cursor(2),
        scopeRef: scope(),
        payload: {
          oneofKind: "activityAppended",
          activityAppended: {
            runId: "run-1",
            activity: RunActivityDescriptorV2.create({ kind: "unknown" }),
          },
        },
      }),
      RunStatusEnvelopeV2.create({
        eventUlid: eventUlid(3),
        cursor: cursor(3),
        scopeRef: scope(),
        payload: {
          oneofKind: "activityAppended",
          activityAppended: { runId: "run-1" },
        },
      }),
    ];

    for (const envelope of envelopes) {
      const watchRunStatus = vi.fn(() => ({
        responses: (async function* () {
          yield envelope;
        })(),
      }));
      const api = apiWith({ watchRunStatus });
      await expect(
        api.streamUpdates("operator-1", eventUlid(1))[Symbol.asyncIterator]().next(),
      ).rejects.toThrow(/activity kind|run or descriptor/);
    }
  });

  it("accepts a stable catalog when non-catalog updates advance the run baseline", async () => {
    const summary = RunSummaryMsg.create({ runId: "run-1", revision: "2" });
    const discoverFlows = vi.fn(() => ({
      response: Promise.resolve(
        FlowListV2.create({ cursor: cursor(0, "flows"), scopeRef: scope() }),
      ),
    }));
    const listRunSummaries = vi.fn(() => ({
      response: Promise.resolve(
        RunSummaryPageV2.create({
          cursor: cursor(2),
          scopeRef: scope(),
          runs: [RunSummaryV2.create({ runId: summary.runId, revision: summary.revision })],
        }),
      ),
    }));
    const api = apiWith({ discoverFlows, listRunSummaries });

    const baseline = await api.loadBaseline();

    expect(baseline.catalog).toMatchObject({
      operatorInstanceId: "operator-1",
      asOfEventUlid: eventUlid(0),
    });
    expect(baseline.asOfEventUlid).toBe(eventUlid(2));
    expect(baseline.runs).toEqual([summary]);
  });

  it("requests exactly one typed activity page for logs and events", async () => {
    const signal = new AbortController().signal;
    const log = LogRecordDescriptorMsg.create({
      sequence: "10",
      nodeId: "agent",
      bodyToken: "log-body",
    });
    const event = AgentEventDescriptorMsg.create({
      eventSequence: "3",
      bodyToken: "event-body",
    });
    const logPage = continuation("log-page", 20, "activity:run-1:logs");
    const eventPage = continuation("event-page", 20, "activity:run-1:agent");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor(20, "run:run-1"),
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
          cursor: cursor(20, request.nodeId ? "activity:run-1:agent" : "activity:run-1:logs"),
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
            20,
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
      expectedAsOfEventUlid: eventUlid(20),
    };
    const eventRequest: AgentEventPageRequest = {
      pageToken: "event-page",
      afterEventSequence: "2",
      beforeEventSequence: "0",
      pageSize: 30,
      order: DescriptorPageOrder.FORWARD,
      expectedOperatorInstanceId: "operator-1",
      expectedAsOfEventUlid: eventUlid(20),
      expectedRunId: "run-1",
      expectedNodeId: "agent",
    };

    await expect(api.listLogPage(logRequest, signal)).resolves.toMatchObject({
      operatorInstanceId: "operator-1",
      asOfEventUlid: eventUlid(20),
      records: [log],
      nextPageToken: "log-next",
      nextCursor: "10",
    });
    await expect(api.listAgentEventPage(eventRequest, signal)).resolves.toMatchObject({
      operatorInstanceId: "operator-1",
      asOfEventUlid: eventUlid(20),
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

  it("rejects a nonmatching activity kind in a page mapping", async () => {
    const logPage = continuation("log-page", 20, "activity:run-1:logs");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor(20),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1" }),
          logContinuation: logPage,
        }),
      ),
    }));
    const listRunActivity = vi.fn(() => ({
      response: Promise.resolve(
        RunActivityPageV2.create({
          cursor: cursor(20),
          runId: "run-1",
          scopeRef: scope(),
          activities: [
            RunActivityDescriptorV2.create({
              kind: "agent_event",
              detailRef: detailRef("event-body"),
            }),
          ],
        }),
      ),
    }));
    const api = apiWith({ getRunSnapshot, listRunActivity });
    await api.getLatestRunSnapshot("run-1", "operator-1");

    await expect(
      api.listLogPage({
        pageToken: "log-page",
        afterSequence: "0",
        beforeSequence: "0",
        pageSize: 25,
        nodeId: "",
        order: DescriptorPageOrder.FORWARD,
        expectedOperatorInstanceId: "operator-1",
        expectedAsOfEventUlid: eventUlid(20),
      }),
    ).rejects.toThrow("Expected log activity");
  });

  it("rejects a page that cannot advance its continuation", async () => {
    const samePage = continuation("same-page", 20, "activity:run-1:logs");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor(20, "run:run-1"),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1" }),
          logContinuation: samePage,
        }),
      ),
    }));
    const listRunActivity = vi.fn(() => ({
      response: Promise.resolve(
        RunActivityPageV2.create({
          cursor: cursor(20, "activity:run-1:logs"),
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
        expectedAsOfEventUlid: eventUlid(20),
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
    for await (const update of api.streamUpdates("operator-1", eventUlid(9), signal)) {
      updates.push(update);
    }

    expect(updates).toEqual([]);
    expect(getRunSnapshot).toHaveBeenCalledWith({ runId: "run-1" }, { abort: signal });
    expect(watchRunStatus).toHaveBeenCalledWith(
      { scopeRef: { reference: "operator-1" } },
      { abort: signal },
    );
  });

  it("does not let a flow cursor overwrite an event cursor with the same sequence", async () => {
    const eventCursor = cursor(7, "operator-events");
    const watchRunStatus = vi.fn(() => ({
      responses: (async function* () {
        yield RunStatusEnvelopeV2.create({
          eventUlid: eventUlid(7),
          cursor: eventCursor,
          scopeRef: scope(),
          payload: {
            oneofKind: "flowListChanged",
            flowListChanged: {
              flowList: FlowListV2.create({
                cursor: cursor(7, "flows"),
                scopeRef: scope(),
              }),
            },
          },
        });
      })(),
    }));
    const api = apiWith({ watchRunStatus });

    for await (const update of api.streamUpdates("operator-1", eventUlid(0))) {
      expect(update).toBeDefined();
    }
    for await (const update of api.streamUpdates("operator-1", eventUlid(7))) {
      expect(update).toBeDefined();
    }

    expect(watchRunStatus).toHaveBeenNthCalledWith(
      1,
      { scopeRef: { reference: "operator-1" } },
      undefined,
    );
    expect(watchRunStatus).toHaveBeenNthCalledWith(
      2,
      { afterCursor: eventCursor, scopeRef: { reference: "operator-1" } },
      undefined,
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
    const jsonRef = detailRef("json-body");
    const textRef = detailRef("text-body");
    const getRunSnapshot = vi.fn(() => ({
      response: Promise.resolve(
        RunSnapshotV2.create({
          cursor: cursor(1, "run:run-1"),
          scopeRef: scope(),
          summary: RunSummaryV2.create({ runId: "run-1" }),
          nodes: [
            NodeSnapshotV2.create({
              nodeId: "json",
              trace: TraceDescriptorV2.create({
                available: true,
                revision: "1",
                detailRef: jsonRef,
              }),
            }),
            NodeSnapshotV2.create({
              nodeId: "text",
              trace: TraceDescriptorV2.create({
                available: true,
                revision: "1",
                detailRef: textRef,
              }),
            }),
          ],
        }),
      ),
    }));
    const api = apiWith({ readActivityDetail, getRunSnapshot });
    await api.getLatestRunSnapshot("run-1", "operator-1", signal);

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
    expect(readActivityDetail).toHaveBeenNthCalledWith(1, expect.anything(), { abort: signal });
    expect(readActivityDetail).toHaveBeenNthCalledWith(2, expect.anything(), { abort: signal });
  });
});
