## Context

The operator already watches source paths and calls `WorkflowRegistry.rescan`, which atomically replaces the current `CatalogView`. That view is intentionally current-only and is not published through the update stream. The run worker already emits the prepared workflow's node IDs, graph, node types, and display names, but `_run_from_prepared` retains only per-node name and type in `RunState`; edges and ordering are discarded. `RunSnapshot` consequently cannot reconstruct a historical DAG.

The gRPC protocol currently separates catalog listing from a stream of run-only updates. Browser JavaScript cannot consume the existing native gRPC server directly; it needs a browser transport adapter such as gRPC-Web or an HTTP/SSE gateway.

## Goals / Non-Goals

**Goals:**
- Establish one immutable, execution-derived topology for every run.
- Publish complete, atomically replaceable current-catalog revisions to interactive clients.
- Make current-workflow and historical-run views independent but navigable from the same UI.
- Preserve the existing local-first, loopback-default operator security model.

**Non-Goals:**
- Editing workflow source or composing DAGs in the browser. Mutating a running workflow when its source changes.
- Durable run history or workflow-version storage across operator restarts.
- Production authentication, multi-tenancy, or public Internet serving.
- Publishing workflow `BaseInput` schemas, generating input forms, or providing schema-aware JSON editing.

## Decisions

### Retain a topology snapshot with each run

Introduce a frozen workflow-topology value containing only node ID order, adjacency graph, node types, and display names. Construct it from the worker's `prepared` event, not the current catalog descriptor: the worker event represents what that run actually loaded and executed. Store it on the in-memory run record and include it in the structural run snapshot and transport representation.

`RunState.nodes` remains execution state keyed by node ID. The topology snapshot is the rendering and identity layer. A run view joins the two; it never reads the current `WorkflowInfo` to supply missing nodes or edges.

```
current catalog                      run record
───────────────                      ──────────
WorkflowDescriptor revision N        RunState + TopologySnapshot
        │                              captured from prepared workflow
        ▼                                       │
workflow view                                   ▼
                                   run view = topology + execution state
```

This also handles a source change between catalog discovery and worker preparation: the run view reports the topology the worker emitted, not a potentially stale catalog descriptor.

### Retain bounded node failure messages

The run worker already reports a bounded `error` string with `node_failed`. Retain that value on node execution state and carry it in structural run snapshots and live node-status updates. The run inspector displays the message with status and timing. Do not retain exception objects or add traceback capture in v1.

### Treat reload as a new immutable catalog revision

Discovery builds a candidate catalog off-lock. The operator validates it, replaces the current catalog in one operation, assigns a monotonically increasing catalog revision, and publishes the resulting full catalog snapshot plus diagnostics. Clients use the revision to discard older updates and replace their current-workflow state wholesale.

A failed or invalid candidate preserves the last successful catalog and publishes diagnostics rather than a partial replacement. This changes the present `WorkflowRegistry.rescan` behavior, whose own documentation says it does not retain a last-good descriptor.

Catalog changes are independent of run updates: an active run keeps its topology snapshot; the new catalog is used for subsequent selections and run starts.

### Publish scan targets as catalog metadata

Project each existing `ConfiguredRoot` into an immutable `ScanTargetInfo` containing its stable alias, normalized target path, and `file` or `directory` kind. Add the scan-target collection to initial catalog reads, live catalog replacements, and reset baselines. Workflows already carry `root_alias`, which is the only join key the browser needs.

This is a read-only client projection of operator configuration. It does not change discovery, workflow identity, source watching, or execution.

### Extend the live transport to include catalog revisions

The current `StreamRunUpdates` envelope represents only run changes. Replace it with an operator-update stream/envelope that can carry either a run update, a complete catalog revision, or an existing reset notice. Migrate the TUI client to the new stream at the same time; do not retain a parallel legacy stream.

A reconnect or reset reloads a consistent baseline: the latest catalog revision and the retained run summaries/snapshots. Catalog events carry a full replacement view rather than an incremental graph diff, avoiding client-side patch ordering and deletion edge cases. The web UI can animate before/after topologies locally using stable node IDs.

### Use gRPC-Web for browser transport

The web client SHALL use generated TypeScript stubs from `operator.proto` through an in-process gRPC-Web-capable adapter owned by the operator. One `ava operator` process hosts the existing native gRPC listener for Python/TUI clients and an optional browser-facing listener for compiled assets and gRPC-Web unary/server-streaming requests. Both listeners use the same authoritative `Operator` instance; there is no second operator, duplicated state, required sidecar process, or inter-process synchronization.

This keeps `operator.proto` as the sole public operation and event schema while leaving the operator independently usable without the web listener. The browser listener is loopback-only by default and same-origin with its assets, avoiding default CORS exposure. Non-loopback use remains explicitly delegated to a trusted, authenticated external boundary. If no reliable in-process adapter supports the required streaming semantics, the transport decision MUST be revisited rather than silently introducing an externally managed proxy.

### Use a small React and TypeScript frontend stack

Build the browser client with React, TypeScript, and Vite. Use `@xyflow/react` for the read-only workflow and run canvases, CodeMirror 6 for the optional JSON input editor, and `@tanstack/react-virtual` for large run, log, event, and trace navigators. Enable React Flow's viewport culling for large DAGs.

Keep application state in typed React reducers/hooks organized around the operator's replaceable catalog, run, and detail projections. Do not add Redux, a second client data model, or browser persistence. Network pagination and bounded detail hydration remain mandatory: component virtualization is not a substitute for avoiding eager transport and parsing.

### Use ephemeral remote projections

The operator is authoritative for workflow definitions, runs, details, and lifecycle transitions. The web client keeps only replaceable projections of operator responses and ordered updates; they are bounded rendering caches, not a client-owned source of truth. A reconnect or reset discards/rebuilds these projections from the authoritative baseline.

```
Operator state ── ordered updates ──► ephemeral browser projections
                                          ├─ catalog / selected run cache
                                          ├─ selection and inspector
                                          ├─ viewport pan and zoom
                                          └─ in-flight start/cancel request
```

Starting a run returns an identity but does not authorize the client to construct a local run record. Cancelling a run is likewise a request: both lifecycle displays reconcile only from operator updates. The browser persists neither run history nor artifact bodies; a catalog update cannot erase or morph a historical run projection.

### Keep workflow input advanced and schema-blind

The primary Run action submits no input. A secondary, visually subtle control reveals a raw JSON-object editor for users who already know the workflow's `BaseInput` contract. The editor is closed by default, publishes no browser state beyond its current draft, and sends parsed JSON unchanged through the existing start-run request.

Discovery does not publish a Pydantic/JSON schema in v1. The editor provides syntax validation only; the operator remains authoritative for `BaseInput` validation and returns actionable errors. Workflow-level file/workspace controls and `BaseContext` editing are out of scope.


### Structure the explorer and canvases by view semantics

The Explorer groups current workflows and retained runs under their configured scan target. Selecting a workflow opens a blueprint-styled current-definition canvas; selecting a run opens a distinct execution canvas from that run's topology snapshot. The canvases are read-only and preserve pan/zoom state only ephemerally.

Workflow cards place agent input and output field lists inside their own bounds. DAG edges represent dependency, not individual field bindings: each source-target pair renders at most one arrow. Opening an agent node presents readable instructions first, with model, runtime, skills, and tools as supporting declaration metadata.

Run cards retain the same structural edges but prioritize execution status, duration, and failure state. They do not reuse current agent field declarations, which could be incorrect for a historical run.

### Retain bounded agent invocation inputs and outputs

PredictRLM `run.started` evidence contains actual invocation inputs, but Avalanche currently projects only their field names. Preserve supported input values in that existing event, matching the terminal outputs already projected from `run.succeeded`. The run inspector reads both through existing agent-event and hydrated-trace detail paths and presents separate Inputs and Output views using current declaration metadata only as labels.

This is agent-invocation evidence, not generic DAG-node value capture. Recursively project JSON-shaped values and declared model values into the ordinary `inputs` and `outputs` structures. At this projection boundary, encode an actual `predict_rlm.File` as a tagged JSON value containing its non-empty host path; lists and nested structures retain those tagged values in place. The browser's generic value renderer recognizes the tag and gives that value path-specific presentation. There is no parallel file-reference event or index.

Unsupported or over-limit values are represented as unavailable and MUST NOT be silently converted with `str(...)` or cause an otherwise valid agent invocation to fail. The existing agent evidence observer, run-worker event queue, operator `AgentEvent` storage, detail pagination, and browser transport carry the projected JSON unchanged. Generic operator code does not inspect arbitrary `.path` attributes. The browser does not copy file contents, persist artifacts, validate path existence, or offer a download contract.

### Decompose RunTrace into demand-loaded projections

Preserve the user-visible semantics of PredictRLM's exportable `RunTrace` without transporting it as one monolithic JSON body. Store run-level trace fields as a lightweight header; expose lifecycle evidence through the existing paginated agent-event path; and retain each complete `IterationStep` as the detail body of its `iteration.recorded` event.

Extend `AgentEventDescriptor` with the summary fields needed to build a navigator without reading its body: event kind and, for iteration events, iteration number, duration, error state, tool count, and predict count. `ListAgentEvents` pages these descriptors; the existing `ReadDetail` retrieves only a selected event/turn body. The complete turn body preserves reasoning, code, truncated and untruncated output, tool calls, predict-call groups/subcalls, LM finish metadata, and per-turn usage.

The browser virtualizes descriptor rows and keeps a small bounded LRU of hydrated bodies. Following the live turn is the default; selecting another turn pauses following. Inputs and terminal outputs remain separate views. The web client does not call monolithic `ReadTrace`; migrate the TUI to the same descriptor/detail path so complete trace bodies are not duplicated solely for compatibility.

## Risks / Trade-offs

- Full catalog replacements make reload behavior simple and correct but transmit more data than diffs. Local workflow catalogs are expected to be small; a later scale constraint can justify an explicitly versioned diff protocol.
- Retained topology increases in-memory cost proportional to runs times DAG metadata. It intentionally excludes executable functions and full workflow objects, preserving process/serialization boundaries.
- Last-good catalog retention prevents a typing-error reload from making the UI empty, but it means the displayed catalog can be stale while diagnostics are active; the UI must make that state visible.
- A gRPC-Web adapter adds a runtime component and frontend build integration, but avoids a duplicate HTTP resource and event schema.
