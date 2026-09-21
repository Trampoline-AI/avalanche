import { describe, expect, it, vi } from "vitest";

import { GrpcWebOperatorApi } from "./api";
import type { ClassifierEventPageRequest } from "./api";
import type { ClassifierDeclaration, ClassifierInvocation } from "./classifier";
import {
  ActivityDetailChunkV2,
  ActivityDetailRefV2,
  CatalogReloadRequiredV2,
  ClassifierInvocationSummaryV2,
  ContinuationRefV2,
  FlowInfoV2,
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
  WorkflowTopologyV2,
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

const classifierDeclaration: ClassifierDeclaration = {
  input_schema: { type: "object", properties: { original: { type: "string" } } },
  step_inputs: [],
  step_output: { type_name: "Unspecified", json_schema: null },
  questions: {
    accepted: { type: "noul", instructions: "Original decision", criteria: null },
  },
  runtime: { model: "jev-latest", timeout: 10 },
};
const classifierInvocation: ClassifierInvocation = {
  invocation_id: "classifier-call",
  invocation_index: 0,
  status: "success",
  started_at: 1,
  ended_at: 2,
  declaration: classifierDeclaration,
  input: { request: "Review the current policy", revision: 2 },
  result: {
    model: "jev-latest",
    answers: { accepted: { type: "noul", noul: 0.8 } },
    usage: { input_tokens: 10, output_tokens: 2 },
  },
  error: null,
};
const classifierBody = new TextEncoder().encode(JSON.stringify(classifierInvocation));
const classifierContinuation = ContinuationRefV2.create({
  scopeRef,
  cursor: cursor(),
  continuationId: "classifier-events",
});
const classifierSnapshot = RunSnapshotV2.create({
  scopeRef,
  cursor: cursor(),
  summary: { runId: "run-1" },
  nodes: [{ nodeId: "classify", activityContinuation: classifierContinuation }],
  topology: {
    nodeIds: ["classify"],
    classifierMetadataJson: { classify: JSON.stringify(classifierDeclaration) },
  },
});
const classifierRequest: ClassifierEventPageRequest = {
  pageToken: "classifier-events",
  afterEventSequence: "0",
  beforeEventSequence: "0",
  pageSize: 25,
  order: DescriptorPageOrder.FORWARD,
  expectedOperatorInstanceId: "operator-1",
  expectedAsOfEventUlid: eventUlid(8),
  expectedRunId: "run-1",
  expectedNodeId: "classify",
};

function classifierActivity(sequence = "1"): RunActivityDescriptorV2 {
  return RunActivityDescriptorV2.create({
    activityId: `classifier:classify:${sequence}`,
    kind: "classifier_event",
    nodeId: "classify",
    runSequence: sequence,
    invocationId: classifierInvocation.invocation_id,
    eventKind: classifierInvocation.status,
    classifierSummary: {
      invocationIndex: classifierInvocation.invocation_index,
      answers: [{ questionId: "accepted", answer: { oneofKind: "noul", noul: 0.8 } }],
    },
    durationMs: "1000",
    sizeBytes: String(classifierBody.byteLength),
    detailRef: {
      runId: "run-1",
      scopeRef,
      activityId: `classifier:classify:${sequence}`,
      runSequence: sequence,
      objectUri: `local://detail/classifier-${sequence}`,
      objectKey: `classifier-${sequence}`,
      sha256: "b".repeat(64),
      sizeBytes: String(classifierBody.byteLength),
    },
  });
}

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

  it("keeps historical classifier declarations pinned when the current catalog changes", async () => {
    const revisedDeclaration: ClassifierDeclaration = {
      ...classifierDeclaration,
      input_schema: { type: "object", properties: { revised: { type: "number" } } },
      step_inputs: [
        {
          name: "revision",
          type_name: "int",
          json_schema: { type: "integer" },
          required: true,
        },
      ],
      step_output: { type_name: "bool", json_schema: { type: "boolean" } },
      questions: {
        accepted: { type: "noul", instructions: "Revised decision", criteria: null },
      },
    };
    const api = apiWith({
      discoverFlows: () => ({
        response: Promise.resolve(
          FlowListV2.create({
            scopeRef,
            cursor: cursor(),
            flows: [
              FlowInfoV2.create({
                workflowSelector: "orders",
                classifierMetadataJson: { classify: JSON.stringify(revisedDeclaration) },
                topology: WorkflowTopologyV2.create({
                  classifierMetadataJson: { classify: JSON.stringify(revisedDeclaration) },
                }),
              }),
            ],
          }),
        ),
      }),
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      watchRunStatus: () => ({
        responses: (async function* () {
          yield RunStatusEnvelopeV2.create({
            scopeRef,
            cursor: cursor(9),
            eventUlid: eventUlid(9),
            payload: {
              oneofKind: "runCreated",
              runCreated: {
                summary: classifierSnapshot.summary,
                nodes: classifierSnapshot.nodes,
                topology: classifierSnapshot.topology,
              },
            },
          });
        })(),
      }),
    });
    const historical = await api.getLatestRunSnapshot("run-1", "operator-1");
    const current = await api.getCatalog();
    expect(JSON.parse(current.workflows[0].classifierMetadataJson.classify)).toEqual(
      revisedDeclaration,
    );
    expect(JSON.parse(historical.topology!.classifierMetadataJson.classify)).toEqual(
      classifierDeclaration,
    );
    const createdUpdates = api.streamUpdates("operator-1", eventUlid(8));
    const created = await createdUpdates[Symbol.asyncIterator]().next();
    expect(created.value?.payload).toMatchObject({
      update: {
        change: {
          oneofKind: "runCreated",
          runCreated: {
            topology: { classifierMetadataJson: historical.topology!.classifierMetadataJson },
          },
        },
      },
    });
  });

  it("hydrates classifier pages and live details without interpreting them as agent events", async () => {
    const activity = classifierActivity();
    const liveActivity = classifierActivity("2");
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            activities: [activity],
          }),
        ),
      }),
      watchRunStatus: () => ({
        responses: (async function* () {
          yield RunStatusEnvelopeV2.create({
            scopeRef,
            cursor: cursor(9),
            eventUlid: eventUlid(9),
            payload: {
              oneofKind: "activityAppended",
              activityAppended: { runId: "run-1", activity: liveActivity },
            },
          });
        })(),
      }),
      readActivityDetail: () => ({
        responses: (async function* () {
          yield ActivityDetailChunkV2.create({ data: classifierBody });
        })(),
      }),
    });
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(/not bound/i);
    await api.getLatestRunSnapshot("run-1", "operator-1");
    const page = await api.listClassifierEventPage(classifierRequest);
    expect(page.records).toEqual([
      {
        eventSequence: "1",
        invocationId: "classifier-call",
        invocationIndex: 0,
        answers: [{ questionId: "accepted", type: "noul", noul: 0.8 }],
        eventKind: "success",
        bodyToken: "classifier-1",
        sizeBytes: String(classifierBody.byteLength),
        durationMs: "1000",
        error: false,
      },
    ]);
    expect(page.nextCursor).toBe("1");
    await expect(api.readJsonDetail(page.records[0].bodyToken)).resolves.toEqual(
      classifierInvocation,
    );
    await expect(api.listAgentEventPage(classifierRequest)).rejects.toThrow(
      /expected agent event/i,
    );
    const liveUpdates = api.streamUpdates("operator-1", eventUlid(8));
    const live = await liveUpdates[Symbol.asyncIterator]().next();
    expect(live.value?.payload).toMatchObject({
      update: {
        change: {
          oneofKind: "classifierEventAppended",
          classifierEventAppended: {
            runId: "run-1",
            nodeId: "classify",
            event: {
              eventSequence: "2",
              invocationId: "classifier-call",
              bodyToken: "classifier-2",
              invocationIndex: 0,
              answers: [{ questionId: "accepted", type: "noul", noul: 0.8 }],
            },
          },
        },
      },
    });
    await expect(api.readJsonDetail("classifier-2")).resolves.toEqual(classifierInvocation);
  });

  it("preserves every compact answer, including zero values, on pages and live updates without reading details", async () => {
    const activity = RunActivityDescriptorV2.fromBinary(
      RunActivityDescriptorV2.toBinary(
        RunActivityDescriptorV2.create({
          ...classifierActivity(),
          classifierSummary: {
            invocationIndex: 42,
            answers: [
              { questionId: "category", answer: { oneofKind: "choice", choice: "keep" } },
              { questionId: "approved", answer: { oneofKind: "noul", noul: 0 } },
              { questionId: "quality", answer: { oneofKind: "score", score: 0 } },
              { questionId: "priority", answer: { oneofKind: "score", score: 7 } },
            ],
          },
        }),
      ),
    );
    const readActivityDetail = vi.fn();
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            activities: [activity],
          }),
        ),
      }),
      watchRunStatus: () => ({
        responses: (async function* () {
          yield RunStatusEnvelopeV2.create({
            scopeRef,
            cursor: cursor(9),
            eventUlid: eventUlid(9),
            payload: {
              oneofKind: "activityAppended",
              activityAppended: { runId: "run-1", activity },
            },
          });
        })(),
      }),
      readActivityDetail,
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    const expected = {
      invocationIndex: 42,
      answers: [
        { questionId: "category", type: "choice", choice: "keep" },
        { questionId: "approved", type: "noul", noul: 0 },
        { questionId: "quality", type: "score", score: 0 },
        { questionId: "priority", type: "score", score: 7 },
      ],
    };
    expect((await api.listClassifierEventPage(classifierRequest)).records[0]).toMatchObject(
      expected,
    );
    const liveUpdates = api.streamUpdates("operator-1", eventUlid(8));
    const live = await liveUpdates[Symbol.asyncIterator]().next();
    expect(live.value?.payload).toMatchObject({
      update: { change: { classifierEventAppended: { event: expected } } },
    });
    expect(readActivityDetail).not.toHaveBeenCalled();
  });

  it.each([
    { name: "missing summary", summary: undefined },
    {
      name: "unsafe call number",
      summary: ClassifierInvocationSummaryV2.create({
        invocationIndex: Number.MAX_SAFE_INTEGER + 1,
      }),
    },
    {
      name: "negative call number",
      summary: ClassifierInvocationSummaryV2.create({ invocationIndex: -1 }),
    },
    {
      name: "missing typed answer",
      summary: ClassifierInvocationSummaryV2.create({ answers: [{ questionId: "approved" }] }),
    },
    {
      name: "non-finite answer",
      summary: ClassifierInvocationSummaryV2.create({
        answers: [{ questionId: "quality", answer: { oneofKind: "score", score: Infinity } }],
      }),
    },
    {
      name: "duplicate question",
      summary: ClassifierInvocationSummaryV2.create({
        answers: [
          { questionId: "approved", answer: { oneofKind: "noul", noul: 0 } },
          { questionId: "approved", answer: { oneofKind: "noul", noul: 1 } },
        ],
      }),
    },
    {
      name: "answer on a non-success call",
      eventKind: "running",
      summary: ClassifierInvocationSummaryV2.create({
        answers: [{ questionId: "approved", answer: { oneofKind: "noul", noul: 0 } }],
      }),
    },
  ])(
    "rejects $name on both classifier pages and live updates",
    async ({ summary, eventKind }) => {
      const activity = RunActivityDescriptorV2.create({
        ...classifierActivity(),
        classifierSummary: summary,
        eventKind: eventKind ?? "success",
      });
      const api = apiWith({
        getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
        listRunActivity: () => ({
          response: Promise.resolve(
            RunActivityPageV2.create({
              scopeRef,
              cursor: cursor(),
              runId: "run-1",
              activities: [activity],
            }),
          ),
        }),
        watchRunStatus: () => ({
          responses: (async function* () {
            yield RunStatusEnvelopeV2.create({
              scopeRef,
              cursor: cursor(9),
              eventUlid: eventUlid(9),
              payload: {
                oneofKind: "activityAppended",
                activityAppended: { runId: "run-1", activity },
              },
            });
          })(),
        }),
      });
      await api.getLatestRunSnapshot("run-1", "operator-1");
      await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(/summary/i);
      await expect(
        api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next(),
      ).rejects.toThrow(/summary/i);
      await expect(api.readJsonDetail("classifier-1")).rejects.toThrow(/not bound/i);
    },
  );

  it("keeps expired classifier statuses pageable and replayable alongside retained details", async () => {
    const statuses = ["running", "success", "failed", "cancelled"] as const;
    const expired = statuses.map((status, index) =>
      RunActivityDescriptorV2.create({
        ...classifierActivity(String(index + 1)),
        invocationId: `expired-${status}`,
        eventKind: status,
        classifierSummary: {
          invocationIndex: index,
          answers:
            status === "success"
              ? [{ questionId: "accepted", answer: { oneofKind: "noul", noul: 0.8 } }]
              : [],
        },
        error: status === "failed",
        detailRef: undefined,
      }),
    );
    const retained = classifierActivity("5");
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            activities: [...expired, retained],
          }),
        ),
      }),
      watchRunStatus: () => ({
        responses: (async function* () {
          for (const [index, activity] of expired.entries()) {
            yield RunStatusEnvelopeV2.create({
              scopeRef,
              cursor: cursor(index + 9),
              eventUlid: eventUlid(index + 9),
              payload: {
                oneofKind: "activityAppended",
                activityAppended: { runId: "run-1", activity },
              },
            });
          }
        })(),
      }),
      readActivityDetail: () => ({
        responses: (async function* () {
          yield ActivityDetailChunkV2.create({ data: classifierBody });
        })(),
      }),
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    const page = await api.listClassifierEventPage(classifierRequest);
    expect(page.records.slice(0, 4)).toEqual(
      statuses.map((status, index) => ({
        invocationId: `expired-${status}`,
        invocationIndex: index,
        answers:
          status === "success" ? [{ questionId: "accepted", type: "noul", noul: 0.8 }] : [],
        eventSequence: String(index + 1),
        eventKind: status,
        bodyToken: "",
        sizeBytes: String(classifierBody.byteLength),
        durationMs: "1000",
        error: status === "failed",
      })),
    );
    expect(page.nextCursor).toBe("5");
    await expect(api.readJsonDetail(page.records[4].bodyToken)).resolves.toEqual(
      classifierInvocation,
    );
    const updates = api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]();
    for (const status of statuses) {
      const update = await updates.next();
      expect(update.value?.payload).toMatchObject({
        update: {
          change: {
            oneofKind: "classifierEventAppended",
            classifierEventAppended: {
              event: { invocationId: `expired-${status}`, eventKind: status, bodyToken: "" },
            },
          },
        },
      });
    }
    expect((await updates.next()).done).toBe(true);
  });

  it("rejects present classifier references with no object key instead of treating them as expired", async () => {
    const activity = classifierActivity();
    activity.detailRef!.objectKey = "";
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            activities: [activity],
          }),
        ),
      }),
      watchRunStatus: () => ({
        responses: (async function* () {
          yield RunStatusEnvelopeV2.create({
            scopeRef,
            cursor: cursor(9),
            eventUlid: eventUlid(9),
            payload: {
              oneofKind: "activityAppended",
              activityAppended: { runId: "run-1", activity },
            },
          });
        })(),
      }),
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow();
    await expect(
      api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next(),
    ).rejects.toThrow();
  });

  it("rejects classifier page identities from another selection or an advanced baseline", async () => {
    let response = RunActivityPageV2.create({
      scopeRef,
      cursor: cursor(),
      runId: "run-2",
      activities: [classifierActivity()],
    });
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({ response: Promise.resolve(response) }),
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    await expect(
      api.listClassifierEventPage({
        ...classifierRequest,
        expectedRunId: "run-2",
      }),
    ).rejects.toThrow(/selected node snapshot/i);
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(
      /selected node snapshot/i,
    );
    response = RunActivityPageV2.create({ ...response, runId: "run-1", cursor: cursor(9) });
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(
      /selected node snapshot/i,
    );
    response = RunActivityPageV2.create({
      ...response,
      cursor: cursor(),
      activities: [{ ...classifierActivity(), nodeId: "another-node" }],
    });
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(
      /selected node snapshot/i,
    );
    await expect(api.readJsonDetail("classifier-1")).rejects.toThrow(/not bound/i);
  });

  it("refuses agent activities on classifier pages and cross-run detail references on the live path", async () => {
    const activity = classifierActivity();
    const api = apiWith({
      getRunSnapshot: () => ({ response: Promise.resolve(classifierSnapshot) }),
      listRunActivity: () => ({
        response: Promise.resolve(
          RunActivityPageV2.create({
            scopeRef,
            cursor: cursor(),
            runId: "run-1",
            activities: [{ ...activity, kind: "agent_event" }],
          }),
        ),
      }),
      watchRunStatus: () => ({
        responses: (async function* () {
          yield RunStatusEnvelopeV2.create({
            scopeRef,
            cursor: cursor(9),
            eventUlid: eventUlid(9),
            payload: {
              oneofKind: "activityAppended",
              activityAppended: {
                runId: "run-2",
                activity,
              },
            },
          });
        })(),
      }),
    });
    await api.getLatestRunSnapshot("run-1", "operator-1");
    await expect(api.listClassifierEventPage(classifierRequest)).rejects.toThrow(
      /expected classifier event/i,
    );
    await expect(
      api.streamUpdates("operator-1", eventUlid(8))[Symbol.asyncIterator]().next(),
    ).rejects.toThrow(/bound detail reference/i);
    await expect(api.readJsonDetail("classifier-1")).rejects.toThrow(/not bound/i);
    const agent = await api.listAgentEventPage(classifierRequest);
    expect(agent.records[0].invocationId).toBe("classifier-call");
  });
});
