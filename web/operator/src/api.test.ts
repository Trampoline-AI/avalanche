import { describe, expect, it, vi } from "vitest";

import { GrpcWebOperatorApi } from "./api";
import {
  ActivityDetailChunkV2,
  ActivityDetailRefV2,
  CatalogReloadRequiredV2,
  ContinuationRefV2,
  FlowListV2,
  LifecycleCursorV2,
  NodeSnapshotV2,
  ProjectSummaryCursorV2,
  RunActivityDescriptorV2,
  RunActivityPageV2,
  RunSnapshotV2,
  RunStatusEnvelopeV2,
  RunSummaryPageV2,
  RunSummaryV2,
  ScopeReferenceV2,
  TraceDescriptorV2,
} from "./generated/operator";
import type { IOperatorServiceV2Client } from "./generated/operator.client";
import { DescriptorPageOrder } from "./model";
import { eventUlid } from "./test/fixtures";

function apiWith(client: Partial<Record<keyof IOperatorServiceV2Client, unknown>>) {
  return new GrpcWebOperatorApi("http://operator.test", client as IOperatorServiceV2Client);
}
const scopeRef = ScopeReferenceV2.create({ reference: "operator-1" });
function cursor(sequence = 8) {
  return LifecycleCursorV2.create({
    stream: "operator-events",
    topologyFingerprint: "topology",
    streamGeneration: "1",
    retainedFloorEventUlid: eventUlid(1),
    eventUlid: eventUlid(sequence),
  });
}
const summaryCursor = ProjectSummaryCursorV2.create({
  stream: "project-summaries",
  topologyFingerprint: "summary-topology",
  sourceGeneration: "2026-08-19T14:57:00Z",
  retainedFloorSequence: "9007199254740993",
  targetHeadSequence: "9007199254740995",
  checkpointWatermark: "9007199254740997",
  checkpointDigest: "checkpoint-1",
});
const continuation = ContinuationRefV2.create({
  continuationId: "page-2",
  scopeRef,
  cursor: cursor(),
  projectSummaryCursor: summaryCursor,
});

describe("operator transport boundary", () => {
  it("loads all summary pages without hydrating runs, but refuses to mix checkpoint generations", async () => {
    let changedCheckpoint = false;
    const api = apiWith({
      discoverFlows: () => ({
        response: Promise.resolve(
          FlowListV2.create({ scopeRef, cursor: cursor(), revision: "0" }),
        ),
      }),
      listRunSummaries: (request: { continuation?: ContinuationRefV2 }) => ({
        response: Promise.resolve(
          RunSummaryPageV2.create({
            scopeRef,
            cursor: cursor(),
            projectSummaryCursor:
              changedCheckpoint && request.continuation
                ? { ...summaryCursor, checkpointDigest: "checkpoint-2" }
                : summaryCursor,
            runs: [
              RunSummaryV2.create({
                runId: request.continuation ? "run-2" : "run-1",
                workflowSelector: "flows.py::orders",
              }),
            ],
            nextPage: request.continuation ? undefined : continuation,
          }),
        ),
      }),
      getRunSnapshot: () => {
        throw new Error("Baseline must not hydrate run bodies");
      },
    });
    const baseline = await api.loadBaseline();
    expect(baseline.catalog.revision).toBe("0");
    expect(baseline.runs.map((run) => [run.runId, run.workflowId])).toEqual([
      ["run-1", "flows.py::orders"],
      ["run-2", "flows.py::orders"],
    ]);
    changedCheckpoint = true;
    await expect(api.loadBaseline()).rejects.toThrow(/summary cursor changed/i);
  });

  it.each([
    { scopeRef: { reference: "operator-2" } },
    { cursor: cursor(9) },
    { revision: "2" },
  ])("rejects a catalog page outside its original baseline: %j", async (change) => {
    const firstPage = FlowListV2.create({
      scopeRef,
      cursor: cursor(),
      revision: "1",
      nextPage: continuation,
    });
    const api = apiWith({
      discoverFlows: vi
        .fn()
        .mockReturnValueOnce({ response: Promise.resolve(firstPage) })
        .mockReturnValueOnce({
          response: Promise.resolve(
            FlowListV2.create({
              scopeRef,
              cursor: cursor(),
              revision: "1",
              ...change,
            }),
          ),
        }),
    });
    await expect(api.getCatalog()).rejects.toThrow(/catalog changed/i);
  });

  it("rejects unbound and nonadvancing activity pages rather than looping or reading another run", async () => {
    const page = ContinuationRefV2.create({
      scopeRef,
      cursor: cursor(),
      continuationId: "logs",
    });
    const api = apiWith({
      getRunSnapshot: () => ({
        response: Promise.resolve(
          RunSnapshotV2.create({
            scopeRef,
            cursor: cursor(),
            summary: RunSummaryV2.create({ runId: "run-1" }),
            logContinuation: page,
          }),
        ),
      }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            nextPage: page,
          }),
        ),
      }),
    });
    const request = {
      pageToken: "logs",
      afterSequence: "4",
      beforeSequence: "0",
      pageSize: 25,
      nodeId: "",
      order: DescriptorPageOrder.FORWARD,
      expectedOperatorInstanceId: "operator-1",
      expectedAsOfEventUlid: eventUlid(8),
    };
    await expect(api.listLogPage(request)).rejects.toThrow(/not bound/i);
    await api.getLatestRunSnapshot("run-1", "operator-1");
    await expect(api.listLogPage(request)).rejects.toThrow(/no progress/i);
    await expect(api.getLatestRunSnapshot("run-1", "operator-2")).rejects.toThrow(
      /operator instance/i,
    );
  });

  it("decodes a valid live update and fails closed on unknown payloads, activities, and foreign scopes", async () => {
    const valid = RunStatusEnvelopeV2.create({
      eventUlid: eventUlid(9),
      cursor: cursor(9),
      scopeRef,
      payload: {
        oneofKind: "catalogReloadRequired",
        catalogReloadRequired: CatalogReloadRequiredV2.create({ deploymentId: "release-a" }),
      },
    });
    const messages = [
      valid,
      RunStatusEnvelopeV2.create({ ...valid, payload: { oneofKind: undefined } }),
      RunStatusEnvelopeV2.create({
        ...valid,
        payload: {
          oneofKind: "activityAppended",
          activityAppended: {
            runId: "run-1",
            activity: RunActivityDescriptorV2.create({ kind: "unknown" }),
          },
        },
      }),
      RunStatusEnvelopeV2.create({ ...valid, scopeRef: { reference: "operator-2" } }),
    ];
    const api = apiWith({
      watchRunStatus: () => ({
        responses: (async function* () {
          yield messages.shift()!;
        })(),
      }),
    });
    const stream = api.streamUpdates("operator-1", eventUlid(8));
    const next = await stream[Symbol.asyncIterator]().next();
    expect(next.value?.payload).toMatchObject({
      update: {
        change: { catalogReloadRequired: { deploymentId: "release-a" } },
      },
    });
    for (const error of [/unknown run status payload/i, /activity kind/i, /operator scope/i]) {
      await expect(
        api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next(),
      ).rejects.toThrow(error);
    }
  });

  it("materializes registered details across UTF-8 chunk boundaries and distinguishes text from malformed JSON", async () => {
    const encoder = new TextEncoder();
    const bodies: Record<string, Uint8Array> = {
      json: encoder.encode('{"message":"A😀B"}'),
      text: encoder.encode("plain A😀B: not JSON }"),
    };
    const api = apiWith({
      getRunSnapshot: () => ({
        response: Promise.resolve(
          RunSnapshotV2.create({
            scopeRef,
            cursor: cursor(),
            summary: RunSummaryV2.create({ runId: "run-1" }),
            nodes: Object.keys(bodies).map((key) =>
              NodeSnapshotV2.create({
                nodeId: key,
                trace: TraceDescriptorV2.create({
                  available: true,
                  detailRef: ActivityDetailRefV2.create({
                    runId: "run-1",
                    scopeRef,
                    activityId: key,
                    runSequence: "1",
                    objectUri: `local://detail/${key}`,
                    objectKey: key,
                    sha256: "a".repeat(64),
                    sizeBytes: "0",
                  }),
                }),
              }),
            ),
          }),
        ),
      }),
      readActivityDetail: ({ detailRef }: { detailRef: ActivityDetailRefV2 }) => ({
        responses: (async function* () {
          // Single-byte chunks split every multi-byte codepoint, not just JSON tokens.
          for (const byte of bodies[detailRef.objectKey]) {
            yield ActivityDetailChunkV2.create({ data: new Uint8Array([byte]) });
          }
        })(),
      }),
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    await expect(api.readJsonDetail("json")).resolves.toEqual({ message: "A😀B" });
    await expect(api.readTextDetail("text")).resolves.toBe("plain A😀B: not JSON }");
    await expect(api.readJsonDetail("text")).rejects.toThrow();
    await expect(api.readTextDetail("unregistered")).rejects.toThrow();
  });
});
