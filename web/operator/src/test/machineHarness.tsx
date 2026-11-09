import { createRoot } from "react-dom/client";
import { useState } from "react";
import { WorkflowWorkspace } from "../WorkflowWorkspace";
import { runKeyLabel } from "../runKey";
import type { LogPageRequest, OperatorApi } from "../api";
import type { WaitCard } from "../RunWaitPanel";
import type {
  JsonValue,
  MachineEventReceipt,
  MachineInstance,
  MachinePageRequest,
  MachineSpec,
  MachineTransition,
  MachineVisit,
  MachineVisitEvent,
} from "../machine";
import {
  CatalogSnapshotMsg,
  FlowInfoMsg,
  LogRecordDescriptorMsg,
  OperatorUpdateEnvelope,
  OperatorUpdate,
  type InputFileAttachment,
  type InputFileTarget,
  NodeSnapshotMsg,
  RunSnapshotMsg,
  RunSummaryMsg,
  WorkflowTopologyMsg,
} from "../model";
import "../style.css";

const spec: MachineSpec = {
  schema_version: "avalanche.machine/v1",
  name: "review",
  key_fields: ["repo", "pr"],
  input_schema: {
    type: "object",
    properties: {
      repo: { type: "string" },
      pr: { type: "integer", minimum: 1 },
      priority: { type: "integer", minimum: 1, maximum: 5 },
      document: { $ref: "#/$defs/File" },
      workspace: { type: "object", additionalProperties: true },
    },
    required: ["repo", "pr", "priority"],
    $defs: {
      File: { type: "object", properties: { kind: { const: "file" } } },
    },
  },
  initial: "review",
  states: {
    review: {
      initial: true,
      final: false,
      work: "review_work",
      defer: ["later"],
      timers: [],
      transitions: [
        {
          event: "amend",
          target: "review",
          payload_schema: {
            type: "object",
            properties: {
              note: { type: "string", minLength: 2 },
              score: { type: "integer", minimum: 0 },
              approved: { type: "boolean" },
              // R2: payloads carry File/Workspace values like run inputs.
              evidence: { anyOf: [{ $ref: "#/$defs/File" }, { type: "null" }], default: null },
              workspace: { type: "object", additionalProperties: true },
            },
            required: ["note", "score", "approved"],
            $defs: {
              File: { type: "object", properties: { kind: { const: "file" } } },
            },
          },
        },
        { event: "advance", target: "ready", payload_schema: null },
      ],
    },
    ready: {
      initial: false,
      final: false,
      work: "ready_work",
      defer: [],
      transitions: [{ event: "back", target: "review", payload_schema: null }],
      timers: [{ target: "closed", delay_seconds: 6 }],
    },
    closed: {
      initial: false,
      final: true,
      work: "cleanup",
      defer: [],
      transitions: [],
      timers: [],
    },
  },
  work_graphs: {},
};
const flow = FlowInfoMsg.create({
  name: "review",
  displayName: "Review machine",
  workflowId: "review",
  machineSpec: spec,
  inputDeclarationJson: JSON.stringify({
    type_name: "ReviewInput",
    json_schema: spec.input_schema,
    consumer_node_ids: [],
  }),
});
// docs/state-machines.md's pr_lifecycle, definition only: a back arc from a shared
// rank and three triggers (two events and a timer) into one state.
const lifecycleState = (
  work: string,
  transitions: [string, string][],
  extra: Partial<MachineSpec["states"][string]> = {},
) => ({
  work,
  initial: false,
  final: false,
  defer: [],
  timers: [],
  transitions: transitions.map(([event, target]) => ({ event, target, payload_schema: null })),
  ...extra,
});
const lifecycleSpec: MachineSpec = {
  schema_version: "avalanche.machine/v1",
  name: "pr_lifecycle",
  key_fields: ["repo", "number"],
  input_schema: {
    type: "object",
    properties: {
      repo: { type: "string" },
      number: { type: "integer" },
      head_sha: { type: "string" },
    },
    required: ["repo", "number", "head_sha"],
  },
  initial: "in_review",
  states: {
    in_review: lifecycleState(
      "review_round",
      [
        ["review_done_with_nits", "amend"],
        ["review_done", "ready_to_merge"],
      ],
      { initial: true },
    ),
    amend: lifecycleState("amend_round", [["amended", "in_review"]]),
    ready_to_merge: lifecycleState(
      "notify_ready",
      [
        ["merged", "closed"],
        ["closed_without_merge", "closed"],
      ],
      { timers: [{ target: "closed", delay_seconds: 2_592_000 }] },
    ),
    closed: lifecycleState("cleanup", [], { final: true }),
  },
  work_graphs: {},
};
const lifecycle = FlowInfoMsg.create({
  name: "pr_lifecycle",
  displayName: "PR lifecycle",
  workflowId: "pr_lifecycle",
  machineSpec: lifecycleSpec,
  inputDeclarationJson: JSON.stringify({
    type_name: "PR",
    json_schema: lifecycleSpec.input_schema,
    consumer_node_ids: [],
  }),
});
const ordinary = FlowInfoMsg.create({
  name: "ordinary",
  displayName: "Ordinary flow",
  workflowId: "ordinary",
  nodeIds: ["fetch", "score", "notify"],
  graph: { fetch: { children: ["score"] }, score: { children: ["notify"] } },
  nodeTypes: { fetch: "step", score: "step", notify: "step" },
  displayNames: { fetch: "fetch", score: "score", notify: "notify" },
  standardStepDocstringLines: {
    fetch: "Load the document under review.",
    score: "Score the document.",
    notify: "Tell the reviewer.",
  },
  // A keyed flow (`key=("id",)`): its runs carry the key as their label, like machines.
  inputDeclarationJson: JSON.stringify({
    type_name: "DocumentInput",
    json_schema: { type: "object", properties: { id: { type: "string" } } },
    consumer_node_ids: ["fetch"],
  }),
});
/** The host's own scoped snapshot for one visit: qualified steps, I/O, logs and .map. */
function visitSnapshot(runId: string, visit: number, status: string): RunSnapshotMsg {
  const id = (name: string) => `v${visit}.${name}`;
  const output = (value: JsonValue) => ({
    inputs: [],
    output: { kind: "json" as const, typeName: "dict", value, truncated: false },
  });
  const now = Date.now() / 1000;
  const step = (name: string, extra: Partial<NodeSnapshotMsg> = {}) =>
    NodeSnapshotMsg.create({
      nodeId: id(name),
      name,
      nodeType: "step",
      status: "success",
      startedAt: now - 3,
      endedAt: now - 1,
      ...extra,
    });
  const interrupted = status === "indeterminate";
  return RunSnapshotMsg.create({
    operatorInstanceId: catalog.operatorInstanceId,
    asOfEventUlid: String(sequence).padStart(26, "0"),
    summary: RunSummaryMsg.create({ runId, workflowId: "review", status }),
    logPageToken: `logs:${runId}:${visit}`,
    expansions: interrupted ? {} : { [id("fanout")]: 2 },
    topology: WorkflowTopologyMsg.create(
      interrupted
        ? { nodeIds: [id("check")], nodeTypes: { [id("check")]: "step" } }
        : {
            nodeIds: [id("check"), id("fanout"), id("notify")],
            graph: {
              [id("check")]: { children: [id("fanout")] },
              [id("fanout")]: { children: [id("notify")] },
            },
            nodeTypes: {
              [id("check")]: "step",
              [id("fanout")]: "step",
              [id("notify")]: "step",
            },
            displayNames: {
              [id("check")]: "check",
              [id("fanout")]: "fanout",
              [id("notify")]: "notify",
            },
            mapSpecs: {
              [id("fanout")]: {
                mappedArgs: ["item"],
                broadcastArgs: [],
                maxItems: 0,
                inherited: false,
                failFast: true,
              },
            },
          },
    ),
    nodes: interrupted
      ? [step("check", { status: "indeterminate", attempts: 2, generation: 1, endedAt: 0 })]
      : [
          step("check", { attempts: 3, generation: 2, stepIo: output({ visit, score: 7 }) }),
          step("fanout"),
          ...[0, 1].map((index) =>
            step(`fanout[${index}]`, {
              mapParent: id("fanout"),
              mapIndex: index,
              stepIo: output({ item: index }),
            }),
          ),
          step("notify", { attempts: 1, generation: 0, stepIo: output({ sent: true }) }),
        ],
  });
}
const catalog = CatalogSnapshotMsg.create({
  operatorInstanceId: "machine-browser",
  revision: "1",
  asOfEventUlid: "00000000000000000000000001",
  workflows: [flow, ordinary, lifecycle],
});
const calls: { method: string; args: JsonValue[] }[] = [];
const instances = new Map<string, MachineInstance>();
const visits = new Map<string, Map<number, MachineVisit>>();
const facts = new Map<string, MachineVisitEvent[]>();
const receipts = new Map<string, MachineEventReceipt>();
const runs = new Map<string, RunSummaryMsg>();
const updates: OperatorUpdate[] = [];
let sequence = 1;
function publish(change: OperatorUpdate["change"]) {
  updates.push(
    OperatorUpdate.create({ eventUlid: String(++sequence).padStart(26, "0"), change }),
  );
}
let failSend = true;
let failRead = false;

function record(method: string, ...args: JsonValue[]) {
  calls.push({ method, args });
}
function instance(runId: string) {
  const result = instances.get(runId);
  if (!result) throw new Error("Machine run not found");
  return result;
}
function enter(
  runId: string,
  state: string,
  source: MachineTransition["source"],
  event: string | null,
) {
  const item = instance(runId);
  const previous = visits.get(runId)!.get(item.visit)!;
  const now = Date.now() / 1000;
  const transition: MachineTransition = {
    from: item.state!,
    to: state,
    visit: item.visit,
    event,
    source,
  };
  previous.left_at = now;
  previous.disposition = "archived";
  previous.transition = transition;
  item.visit++;
  item.state = state;
  item.status = source === "cancel" ? "cancelled" : state === "closed" ? "final" : "waiting";
  item.finished = state === "closed";
  item.accepts = spec.states[state].transitions.map((t) => t.event);
  item.due = state === "ready" ? now + 6 : null;
  visits.get(runId)!.set(item.visit, {
    snapshot: visitSnapshot(runId, item.visit, "success"),
    visit: item.visit,
    state,
    entered_at: now,
    left_at: null,
    disposition: "current",
    transition: null,
    catalog_revision: String(item.visit + 10),
    nodes: {
      [`v${item.visit}.check`]: {
        state: "succeeded",
        kind: "step",
        deps: [],
        g: 2,
        attempts: 3,
      },
      [`v${item.visit}.notify`]: {
        state: "succeeded",
        kind: "step",
        deps: [`v${item.visit}.check`],
        g: 0,
        attempts: 1,
      },
    },
  });
  const summary = runs.get(runId)!;
  summary.status = source === "cancel" ? "cancelled" : item.finished ? "succeeded" : "waiting";
  summary.revision = String(item.visit);
  publish({
    oneofKind: "runStatusChanged",
    runStatusChanged: {
      runId,
      status: summary.status,
      revision: summary.revision,
      startedAt: summary.startedAt,
      endedAt: item.finished ? now : 0,
    },
  });
  if (state === "ready") {
    const visit = item.visit;
    setTimeout(() => {
      if (item.visit === visit && !item.finished) enter(runId, "closed", "timer", null);
    }, 6000);
  }
}
function page<T>(items: T[], request?: MachinePageRequest) {
  const offset = request?.cursor ? Number(request.cursor) : 0;
  const count = request?.limit ?? 25;
  return {
    items: items.slice(offset, offset + count),
    next_cursor: offset + count < items.length ? String(offset + count) : null,
  };
}

class FakeOperatorApi implements OperatorApi {
  async getCatalog() {
    return catalog;
  }
  async loadBaseline() {
    record("baseline");
    return {
      catalog,
      asOfEventUlid: String(sequence).padStart(26, "0"),
      runs: structuredClone([...runs.values()]),
    };
  }
  async getWorkflowNodeSource() {
    return "def work():\n    return 42";
  }
  async getLatestRunSnapshot(runId: string) {
    record("snapshot", runId);
    // Retention removed the history; the summary (status, label) stays readable.
    if (runs.get(runId)?.expired)
      return RunSnapshotMsg.create({
        operatorInstanceId: catalog.operatorInstanceId,
        asOfEventUlid: String(sequence).padStart(26, "0"),
        summary: structuredClone(runs.get(runId)),
      });
    return RunSnapshotMsg.create({
      operatorInstanceId: catalog.operatorInstanceId,
      asOfEventUlid: String(sequence).padStart(26, "0"),
      summary: structuredClone(
        runs.get(runId) ??
          RunSummaryMsg.create({ runId, workflowId: "ordinary", status: "succeeded" }),
      ),
      logPageToken: instances.has(runId) ? "" : `flowlogs:${runId}`,
      topology: WorkflowTopologyMsg.create(
        instances.has(runId)
          ? {}
          : {
              nodeIds: ordinary.nodeIds,
              graph: ordinary.graph,
              nodeTypes: ordinary.nodeTypes,
              displayNames: ordinary.displayNames,
              standardStepDocstringLines: ordinary.standardStepDocstringLines,
              inputDeclarationJson: ordinary.inputDeclarationJson,
            },
      ),
      nodes: instances.has(runId)
        ? []
        : ordinary.nodeIds.map((nodeId, index) =>
            NodeSnapshotMsg.create({
              nodeId,
              name: nodeId,
              nodeType: "step",
              status: "success",
              startedAt: (runs.get(runId)?.startedAt ?? 0) + index,
              endedAt: (runs.get(runId)?.startedAt ?? 0) + index + 1,
              stepIo: {
                inputs: [],
                output: {
                  kind: "json",
                  typeName: "dict",
                  value: { step: nodeId },
                  truncated: false,
                },
              },
            }),
          ),
    });
  }
  async *streamUpdates(_operator: string, _after: string, signal?: AbortSignal) {
    let index = updates.length;
    while (!signal?.aborted) {
      while (index < updates.length) {
        yield OperatorUpdateEnvelope.create({
          operatorInstanceId: catalog.operatorInstanceId,
          payload: { oneofKind: "update", update: updates[index++] },
        });
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 25));
    }
  }
  async listLogPage(request: LogPageRequest) {
    const [scope, runId, visit] = request.pageToken.split(":");
    record("logs", runId ?? "", Number(visit));
    if (scope === "flowlogs")
      return {
        operatorInstanceId: request.expectedOperatorInstanceId,
        asOfEventUlid: request.expectedAsOfEventUlid,
        records: [
          LogRecordDescriptorMsg.create({
            sequence: "1",
            timestamp: Date.now() / 1000,
            level: "INFO",
            nodeId: "fetch",
            sizeBytes: "18",
            bodyToken: "log:flow",
          }),
        ],
        nextCursor: "",
        nextPageToken: "",
      };
    return {
      operatorInstanceId: request.expectedOperatorInstanceId,
      asOfEventUlid: request.expectedAsOfEventUlid,
      records: visit
        ? [
            LogRecordDescriptorMsg.create({
              sequence: "1",
              timestamp: Date.now() / 1000,
              level: "INFO",
              nodeId: `v${visit}.check`,
              sizeBytes: "32",
              bodyToken: `log:${visit}`,
            }),
          ]
        : [],
      nextCursor: "",
      nextPageToken: "",
    };
  }
  async listRunWaits(runId: string, visit?: number): Promise<WaitCard[]> {
    record("waits", runId, visit ?? null);
    const item = instances.get(runId);
    if (!item || visit === undefined || visit < 2) return [];
    const current = visit === item.visit && !item.finished;
    return [
      {
        nodeId: `v${visit}.approve`,
        kind: "decision",
        state: current ? "waiting" : "resolved",
        schema: {
          type: "object",
          properties: { approve: { type: "boolean" } },
          required: ["approve"],
        },
        prompt: `Approve visit ${visit}?`,
        show: { visit },
        audit: current
          ? undefined
          : { outcome: { approve: true }, reason: "reviewed in proof", at: Date.now() / 1000 },
      },
    ];
  }
  async listAgentEventPage() {
    return {
      operatorInstanceId: catalog.operatorInstanceId,
      asOfEventUlid: catalog.asOfEventUlid,
      runId: "",
      nodeId: "",
      records: [],
      nextCursor: "",
      nextPageToken: "",
    };
  }
  async listClassifierEventPage() {
    return {
      operatorInstanceId: catalog.operatorInstanceId,
      asOfEventUlid: catalog.asOfEventUlid,
      runId: "",
      nodeId: "",
      records: [],
      nextCursor: "",
      nextPageToken: "",
    };
  }
  async listRunEvaluations() {
    return [];
  }
  async readJsonDetail() {
    return null;
  }
  async readTextDetail(token: string) {
    if (token === "log:flow") return "fetched document\n";
    return token.startsWith("log:") ? `visit ${token.slice(4)} check finished\n` : "";
  }
  async cancelRun(runId: string) {
    if (!instances.has(runId)) {
      record("flow-cancel");
      return;
    }
    // A waiting instance closes through the normal run cancel.
    record("cancel", runId);
    enter(runId, "closed", "cancel", null);
  }
  async setScheduleEnabled() {
    return ordinary;
  }
  async getMachineInstance(runId: string) {
    record("get", runId);
    if (failRead) {
      throw new Error("Fixture transient read failure");
    }
    return structuredClone(instance(runId));
  }
  async getMachineHistory(runId: string, request?: MachinePageRequest) {
    record("history", runId, request?.cursor ?? null);
    return structuredClone(
      page(
        [...visits.get(runId)!.values()]
          .flatMap((visit) => (visit.transition ? [visit.transition] : []))
          .reverse(),
        request,
      ),
    );
  }
  async getMachineVisit(runId: string, visit: number) {
    record("visit", runId, visit);
    const result = visits.get(runId)?.get(visit);
    if (!result) throw new Error("Visit unavailable");
    return structuredClone(result);
  }
  async getMachineVisitEvents(runId: string, visit: number, request?: MachinePageRequest) {
    record("facts", runId, visit, request?.cursor ?? null);
    // The first archived window is legitimately empty but still advances.
    if (visit === 1 && request?.cursor === undefined)
      return { items: [], next_cursor: "facts-2" };
    return {
      items: structuredClone(
        facts.get(`${runId}:${visit}`) ?? [
          {
            tseq: 1,
            at: Date.now() / 1000,
            status: "ignored" as const,
            event: "irrelevant",
            receipt_id: "ignored-1",
            source: "external" as const,
          },
          {
            tseq: 2,
            at: Date.now() / 1000,
            status: "dropped" as const,
            event: "later",
            receipt_id: "drop-1",
            source: "external" as const,
          },
        ],
      ),
      next_cursor: null,
    };
  }
  async stageInputFile(file: File, target: InputFileTarget) {
    record(
      "stage",
      target.workflow,
      target.field,
      target.event ?? null,
      file.name,
      await file.text(),
    );
    return {
      name: file.name,
      mediaType: file.type,
      sizeBytes: file.size,
      sha256: "25c6fc35afa1d0990da2ab9bded19e53184cc29a14e1e19c440122c8b1fe3c88",
      objectUri: "",
      objectKey: "",
      inlineBytes: new Uint8Array(await file.arrayBuffer()),
    };
  }
  async startRun(
    machine: string,
    input?: Record<string, unknown>,
    inputFiles?: InputFileAttachment[],
  ) {
    if (machine !== "review") {
      record("flow-start", machine, JSON.parse(JSON.stringify(input ?? null)));
      // R4 for keyed flows: an existing key returns its run, new input ignored.
      const label = typeof input?.id === "string" ? runKeyLabel([input.id]) : undefined;
      const existing = [...runs.values()].find(
        (run) => label !== undefined && run.workflowId === machine && run.label === label,
      );
      if (existing) return existing.runId;
      const runId = `flow-${runs.size + 1}`;
      const now = Date.now() / 1000;
      const summary = RunSummaryMsg.create({
        runId,
        workflowId: machine,
        flowName: machine,
        label,
        status: "succeeded",
        createdSequence: String(runs.size + 1),
        revision: "1",
        triggeredAt: now,
        startedAt: now,
        endedAt: now + 4,
      });
      runs.set(runId, summary);
      publish({
        oneofKind: "runCreated",
        runCreated: { summary: structuredClone(summary), nodes: [] },
      });
      return runId;
    }
    const parsed = input ?? {};
    if (
      typeof parsed.repo !== "string" ||
      typeof parsed.pr !== "number" ||
      typeof parsed.priority !== "number"
    )
      throw new Error("Machine input requires repo, pr and priority");
    const key = [parsed.repo, parsed.pr];
    record(
      "run-start",
      machine,
      JSON.parse(JSON.stringify(parsed)),
      (inputFiles ?? []).map((file) => ({
        field: file.fieldName,
        name: file.name,
        bytes: Array.from(file.inlineBytes ?? []),
      })),
    );

    const existing = [...instances.values()].find(
      (item) => JSON.stringify(item.key) === JSON.stringify(key),
    );
    if (existing) return existing.run_id;
    const run_id = `machine-${instances.size + 1}`;
    const item: MachineInstance = {
      machine,
      key,
      run_id,
      deployment_id: "",
      state: "review",
      status: "interrupted",
      visit: 1,
      version: 1,
      accepts: ["amend", "advance"],
      due: null,
      finished: false,
      machine_spec: spec,
      pending_events: [
        { event: "later", receipt_id: "pending-1", payload: { note: "deferred" } },
      ],
      receipts: [],
    };
    instances.set(run_id, item);
    visits.set(
      run_id,
      new Map([
        [
          1,
          {
            visit: 1,
            state: "review",
            snapshot: visitSnapshot(run_id, 1, "indeterminate"),
            entered_at: Date.now() / 1000,
            left_at: null,
            disposition: "indeterminate",
            transition: null,
            catalog_revision: "10",
            nodes: {
              "v1.check": { kind: "step", state: "indeterminate", deps: [], g: 1, attempts: 2 },
            },
          },
        ],
      ]),
    );
    const summary = RunSummaryMsg.create({
      runId: run_id,
      workflowId: machine,
      flowName: machine,
      label: runKeyLabel(key),
      status: "interrupted",
      createdSequence: String(instances.size),
      revision: "1",
      triggeredAt: Date.now() / 1000,
    });
    runs.set(run_id, summary);
    publish({
      oneofKind: "runCreated",
      runCreated: { summary: structuredClone(summary), nodes: [] },
    });
    return run_id;
  }
  async sendMachineEvent(
    runId: string,
    event: string,
    payload: JsonValue,
    idempotencyKey: string,
    inputFiles?: InputFileAttachment[],
  ) {
    record(
      "send",
      runId,
      event,
      payload,
      idempotencyKey,
      (inputFiles ?? []).map((file) => ({
        field: file.fieldName,
        name: file.name,
        bytes: Array.from(file.inlineBytes ?? []),
      })),
    );
    // Files bind before the receipt: an unknown payload field is refused, not dropped.
    const declared = spec.states[instance(runId).state!]?.transitions.find(
      (t) => t.event === event,
    )?.payload_schema?.properties;
    for (const file of inputFiles ?? [])
      if (!declared || !Object.hasOwn(declared, file.fieldName))
        throw new Error(`Event ${event} declares no payload field ${file.fieldName}`);
    const prior = receipts.get(idempotencyKey);
    if (prior) return { ...prior, duplicate: true };
    const item = instance(runId);
    if (
      event === "amend" &&
      (payload === null ||
        Array.isArray(payload) ||
        typeof payload !== "object" ||
        typeof payload.note !== "string" ||
        typeof payload.score !== "number" ||
        typeof payload.approved !== "boolean")
    )
      throw new Error("Payload did not satisfy the event schema");
    const result: MachineEventReceipt = {
      receipt_id: `receipt-${receipts.size + 1}`,
      key: event,
      accepted_at: Date.now() / 1000,
      status: item.accepts.includes(event) ? "accepted" : "ignored",
      duplicate: false,
    };
    receipts.set(idempotencyKey, result);
    item.receipts!.push(result);
    const atVisit = item.visit;
    facts.set(`${runId}:${atVisit}`, [
      {
        tseq: 1,
        at: result.accepted_at,
        status: result.status,
        event,
        receipt_id: result.receipt_id,
        source: "external",
      },
    ]);
    if (result.status === "accepted")
      enter(
        runId,
        spec.states[item.state!].transitions.find((t) => t.event === event)!.target,
        "external",
        event,
      );
    // Simulate a committed send whose response was lost. Retrying must use the same key.
    if (failSend) {
      failSend = false;
      throw new Error("Fixture lost response; retry the same payload");
    }
    return structuredClone(result);
  }
  async retryMachineVisit(runId: string) {
    record("retry", runId);
    enter(runId, instance(runId).state!, "internal", null);
  }
  async fireNextMachineTimer(runId: string) {
    record("fire", runId);
    enter(runId, "closed", "timer", null);
  }
}

const api: OperatorApi = new FakeOperatorApi();
const params = new URLSearchParams(location.search);
const readOnly = params.has("readonly");
const expired = params.has("expired");
if (expired) {
  // R5: a finished machine run and a keyed flow run whose history expired by run
  // policy. Their run, status and key label stay; history reads back `expired`.
  await api.startRun("review", { repo: "acme/api", pr: 7, priority: 1 });
  enter("machine-1", "closed", "external", "advance");
  for (const visit of visits.get("machine-1")!.values()) {
    visit.disposition = "expired";
    delete visit.snapshot;
    visit.nodes = {};
  }
  runs.get("machine-1")!.expired = true;
  await api.startRun("ordinary", { id: "doc-3" });
  runs.get("flow-2")!.expired = true;
  calls.length = 0;
}
if (readOnly) {
  await api.startRun("review", { repo: "acme/api", pr: 1, priority: 1 });
  calls.length = 0;
  Object.assign(api, {
    sendMachineEvent: undefined,
    retryMachineVisit: undefined,
    fireNextMachineTimer: undefined,
  });
}
declare global {
  interface Window {
    machineHarness: { calls: typeof calls; failNextRead: () => void; recoverRead: () => void };
  }
}
window.machineHarness = {
  calls,
  failNextRead: () => {
    failRead = true;
  },
  recoverRead: () => {
    failRead = false;
  },
};
function App() {
  const [selectedRunId, select] = useState<string | undefined>(
    readOnly || expired ? "machine-1" : (params.get("run") ?? undefined),
  );
  const [workflowId, setWorkflow] = useState(params.get("workflow") ?? "review");
  return (
    <div className="avalanche-operator-ui h-screen bg-canvas text-ink">
      <nav className="flex h-10 gap-4 border-b border-line px-4 text-xs">
        <button
          data-testid="machine-nav"
          onClick={() => {
            setWorkflow("review");
            select(undefined);
          }}
        >
          Machine
        </button>
        <button
          data-testid="flow-nav"
          onClick={() => {
            setWorkflow("ordinary");
            select(undefined);
          }}
        >
          Flow
        </button>
      </nav>
      <div style={{ height: "calc(100vh - 40px)" }}>
        <WorkflowWorkspace
          api={api}
          workflowId={workflowId}
          navigation={{ selectedRunId, onSelectRun: select }}
          runActionsEnabled={!readOnly}
        />
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
