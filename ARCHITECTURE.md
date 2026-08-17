# Architecture

Avalanche is a local-first system for defining typed Python workflow DAGs,
executing them locally or through Ray, and controlling those runs through a
local operator. The system has explicit authoring, execution, control-plane,
and presentation boundaries. It does not provide production authentication,
multi-tenancy, durable recovery, or a deployment control plane.

## System at a glance

```text
                         authoring and execution plane
┌──────────────────────────────────────────────────────────────────────────┐
│ Python workflow module                                                    │
│  @workflow + @source/@step/@agent_step/@dest                              │
│          │                                                                │
│          ├── embedded: Workflow.run() → RunHandle → LocalExecutor/Ray     │
│          │                                                                │
│          └── operator: discovery worker → descriptor catalog              │
└──────────┼───────────────────────────────────────────────────────────────┘
           │ serializable descriptors and source locations
           v
┌──────────────────────────────────────────────────────────────────────────┐
│ Local operator                                                           │
│  WorkflowRegistry · discovery cache · Python source watcher · scheduler   │
│  webhook ingress · gRPC server · bounded run/log/detail state             │
│          │                                                                │
│          ├── spawn one coordinator process per run                        │
│          │       └── Workflow.run() → LocalExecutor or RayExecutor        │
│          │                                                                │
│          └── gRPC OperatorServiceV2                                       │
└───────┬───────────────────────────────────────────────────────┬──────────┘
        │ native gRPC                                            │ loopback HTTP +
        │                                                        │ gRPC-Web proxy
        v                                                        v
┌─────────────────────┐                              ┌──────────────────────┐
│ CLI and Textual TUI │                              │ Browser UI           │
│ ava run/result/tui  │                              │ ava web / ava dev     │
└─────────────────────┘                              └──────────────────────┘
```

The operator is the control plane. It owns discovery, run coordination,
operator-visible state, result retention, scheduling, webhook routing, and
updates. Executors are the execution plane. The browser UI and TUI are clients;
neither imports workflow modules or executes user tasks itself.

## Local modes

### Embedded execution

An application can build and run a workflow directly:

```python
run = workflow().run(executor=ava.LocalExecutor())
result = run.result()
```

`Workflow.run()` returns a process-local, awaitable `RunHandle` immediately. A
non-daemon driver thread schedules the DAG. `.result()` blocks in synchronous
code; `await run` waits without blocking an event loop. Cancellation is
cooperative: the driver stops submitting later work after cancellation, but it
does not forcibly interrupt an active local function or Ray task.

No operator, gRPC service, browser UI, or TUI participates in embedded mode.
The handle is not a durable run registry or recovery mechanism.

### Operator-managed execution

```text
CLI / browser UI / TUI
        │
        ▼
   OperatorServiceV2 (gRPC)
        │
        ▼
  Operator parent process
        │ spawn
        ▼
  Per-run coordinator process
        │
        ▼
 Workflow.run() → selected executor
```

Use the operator when flows need discovery, local lifecycle control, scheduled
or webhook-triggered runs, observability, or a UI:

```bash
# From the repository root
uv run ava operator --flows examples --port 7433

# In another terminal
uv run ava web --connect localhost:7433
# or
uv run ava tui --connect localhost:7433
```

`ava dev --flows examples` is the browser-oriented convenience command. It
starts an operator subprocess and a connected `ava web` subprocess; the browser
UI listens on `http://127.0.0.1:7435` by default. It does not start the TUI.

## Workflow authoring and runtime

### DAG construction

`@ava.workflow` runs its decorated Python function in a `ContextVar`-scoped
construction context. Calls to decorated node functions create `NodeFuture`
instances rather than execute user code. `>>` expresses dependency order and
`&` groups independent branches. The resulting `Workflow` contains node
instances, an adjacency graph, stable node slugs, declared input/context types,
and optional scheduling, webhook, and agent-default metadata.

The constructor validates that the graph is acyclic. A workflow body should
describe graph edges; it should not do runtime I/O or invoke models directly.

```text
@workflow construction
  source() >> (agent_a() & agent_b()) >> compose() >> publish()
       │              │                    │            │
       └────── DAG edges and NodeFutures ──┴────────────┘
```

### Runtime binding

The driver schedules nodes topologically. It binds ordinary upstream values and
resolves runtime-provided parameters at execution time. Providers cover run
input and context, streams, cursors, logging, injected agents, and related
runtime capabilities. Provider markers remain references during DAG
construction; they are not eager values.

A workflow can use plain deterministic nodes and `@ava.agent_step` nodes in the
same DAG. An agent step is still an ordinary Avalanche node: Avalanche injects
the keyword-only `ava.Agent`, the step calls it, then the step body validates,
composes, and persists its own result. Agent execution is implemented through
PredictRLM; the step's signature, skills, tools, and runtime model settings
remain explicit declaration data.

Workflow-level `agent_defaults` supply shared runtime settings such as `lm` and
`sub_lm`. A value declared on an individual agent step wins over the workflow
default, which wins over Avalanche and PredictRLM defaults. Signatures, skills,
and tools stay step-local capabilities.

### Executors

`LocalExecutor` executes submitted node functions in the local coordinator
process. `RayExecutor` submits work to Ray when selected. The DAG runtime, not
the UI or operator parent, controls dependency order and binding. Ray object
references remain distributed until a consumer needs materialization; the
operator parent receives lifecycle events and encoded terminal results rather
than live task objects.

For an operator run, executor construction happens in the spawned coordinator:

```text
Operator parent
  └── coordinator imports one workflow module and builds one Workflow
        ├── LocalExecutor: local node execution with queue-backed observers
        └── RayExecutor: Ray runtime environment + Ray log queue
```

The coordinator tears down its executor after a terminal outcome. The parent
coordinates cancellation and process-group shutdown; it does not send a live
`Workflow` object across the process boundary.

### Storage and execution services

Avalanche's storage boundary is backend-neutral. `Namespace` and `Table`
define typed contracts; Iceberg and Lance implement their respective backends.
Applications declare the namespace name, table schema, storage location, and
catalog configuration. Avalanche does not impose a universal filesystem default
for application catalogs.

`ava.Stream` and `ava.Cursor` are runtime providers for workflow data flow.
They are distinct from execution services. Execution services are an
executor-owned task lifecycle around platform-provided resources:

```text
probe → negotiate → open → materialize_input → user task → finalize → teardown
                                      │                         │
                                      └──── abort on failure ────┘
```

The service request is serializable, immutable executor metadata. It must not
contain credentials, live handles, or worker-affinity state. Terminal service
receipts are exposed through the embedded `RunHandle`; they are separate from
workflow payloads and operator result transport.

The repository examples use a local-development convention, not an Avalanche
storage default:

```text
.avalanche/cache/operator/                 discovery cache
.avalanche/catalogs/<workflow>/<pid>/      example warehouses and SQLite catalogs
.avalanche/outputs/<workflow>/<pid>/       generated example artifacts
```

## Operator control plane

### Discovery catalog

`WorkflowRegistry` receives configured `--flows` paths. A file target is one
candidate; a directory target is recursively scanned for public `*.py` files.
Private files beginning with `_` and hidden/generated directories are skipped.
Each candidate is discovered in a short-lived process, which:

1. imports the module through its normal package path when applicable;
2. finds public functions marked with `@ava.workflow`;
3. calls each builder to construct its DAG;
4. validates workflow metadata, schedules, and webhook routes;
5. produces serializable descriptors, topology, agent metadata, diagnostics,
   and imported Python dependency paths.

The operator stores descriptor cache documents under
`./.avalanche/cache/operator/`, keyed by configured roots and invalidated by the
Python environment and source metadata. The cache contains descriptors and
paths, never live workflow callables.

Discovery imports arbitrary configured source. Workflow modules and builders
must be import-safe and side-effect-light. Do not scan a repository root unless
it is deliberately a flow-only tree:

```bash
# Avoid from a repository root containing unrelated Python.
uv run ava operator --flows .

# Prefer a specific module or flow directory.
uv run ava operator --flows examples
```

### Live reload and scheduled metadata

The live watcher observes only non-excluded Python files below resolved import
roots. A changed workflow module or imported Python helper triggers targeted
discovery; adding or deleting a Python candidate updates the catalog without
reimporting unrelated candidates.

Live watching intentionally ignores non-Python changes. If a workflow reads an
import-time resource such as JSON to define a cron expression, restart the
operator after changing that resource. Startup cache validation is broader than
the live Python watcher, so a restart re-evaluates changed local source
metadata.

The scheduler reconciles discovered `cron` declarations with the catalog. It
starts runs through the same operator lifecycle as manual requests. Workflow
descriptors can also declare webhook routes. When routes exist, the operator
starts a loopback HTTP ingress on port `7434` by default; valid JSON POST bodies
become workflow input and return an accepted run ID.

### Run coordination

The operator parent owns the authoritative local projection of each run:
`RunState`, per-node `NodeState`, retained logs, agent-event descriptors, trace
descriptors, update sequences, and a bounded structural baseline. It launches
each run using the `spawn` multiprocessing context.

```text
1. Client calls StartRun.
2. Parent resolves a catalog descriptor to import root + module path + builder symbol.
3. Parent creates queue/events and a private result bundle, then spawns a coordinator.
4. Coordinator imports the module, builds the workflow, and sends preparation metadata.
5. Parent publishes REQUESTING then PENDING state and permits execution.
6. Coordinator runs Workflow.run(...).result() with lifecycle hooks.
7. Queue events update parent-side node/run state, logs, agent events, and traces.
8. Coordinator publishes an encoded terminal result into the private bundle.
9. Parent validates, retains, and serves that result until expiry or cleanup.
```

Preparation, runtime, and terminal events are serializable queue messages. The
parent validates them before mutating its state. The coordinator forwards stdout,
stderr, logging, and agent evidence through the event channel. For Ray runs, a
Ray queue carries worker log events back to the coordinator and then the parent.

Cancellation sets the child cancellation event and eventually tears down the
owned process group. It remains cooperative at the workflow-node level, while
the operator owns final process cleanup.

### Results and retention

A successful coordinator result is encoded into JSON plus separate `File`
attachments. The parent-owned `ResultStore` stores it in a private local result
bundle, validates the manifest and attachment limits, and exposes it through
`GetRunResult`. `ava result RUN_ID --output-dir PATH` retrieves the result into
a new caller-owned directory.

This store is local, bounded, and retention-limited. It is not durable workflow
state or crash recovery. Application-owned tables and output destinations remain
outside it.

## Transport and clients

### gRPC operator contract

`src/runtime/operator/proto/operator.proto` is the transport boundary. The
operator exposes a single native service, `OperatorServiceV2`:

- `DiscoverFlows` for workflows, topology, scan targets, and diagnostics;
- `StartRun`, `CancelRun`, and `GetRunResult` for control and terminal output;
- `ListRunSummaries` and `GetRunSnapshot` for bounded structural state;
- `ListRunActivity` and `ReadActivityDetail` for on-demand run detail (logs,
  agent events, and traces share one activity model);
- `ListRunOutputArtifacts` and `ReadRunOutputArtifact` for result file bodies;
- `WatchRunStatus` for sequenced flow and run updates.

Clients begin from a scope reference and a lifecycle cursor. Update stream
replay is bounded. An operator restart, foreign stream generation, stale
cursor, or slow-consumer overflow requires a client to discard incremental
assumptions and reload a fresh structural baseline (`ResetRequiredV2`). Detail
pages are continuation-bound and bodies are read through scope-bound object
references; they are fetched only for the selected run/node rather than
included in every update. `StartRun` requires a caller-supplied `run_id` as
its idempotency key; local loopback operators accept inline attachment bodies
and reject staged object URIs.

The default ports are:

| Endpoint | Default | Purpose |
| --- | ---: | --- |
| gRPC operator | `7433` | discovery, run control, snapshots, updates, results |
| webhook ingress | `7434` | loopback JSON webhook routes when declared |
| browser UI | `7435` | static browser UI and gRPC-Web proxy |

### Browser UI

The browser UI source lives in `web/operator/` and is built with Vite, React,
and generated TypeScript protobuf bindings. Packaged static assets live in
`src/runtime/operator/web_assets/`.

`ava web` starts a loopback HTTP server that serves those assets and proxies
browser gRPC-Web requests to the configured native gRPC operator. The browser
does not receive a direct Python or gRPC channel to user workflow code.

```text
Browser SPA
  ├── DiscoverFlows + paginated run-summary baseline
  ├── WatchRunStatus from the current cursor
  ├── fetch selected snapshots, logs, agent events, and details on demand
  └── StartRun / CancelRun actions
          │ gRPC-Web
          ▼
ava web listener ── native gRPC ── OperatorServiceV2
```

The browser state layer checks operator instance IDs and sequence boundaries
before merging data. A reset reloads the catalog and run baseline rather than
attempting to merge incompatible state.
Workflow reload status and discovery diagnostics are transport updates too, so
the browser can show an in-progress reload and surface discovery failures
without treating an old catalog as current.

The HTTP listener defaults to loopback. Non-loopback exposure requires
`--trusted-proxy`; Avalanche itself does not turn that flag into authentication
or multi-tenant security.

### Textual TUI

The TUI remains a separate local client. `AvalancheApp` and `UIStore` own
Textual rendering and mutable UI state. `GrpcStateProvider` maps the TUI's
`StateProvider` boundary to the operator's gRPC contract. Worker threads enqueue
updates for the Textual event loop; they do not mutate widgets directly.

`ava tui` without `--connect` uses `MockStateProvider` for local UI work.
`ava tui --connect localhost:7433` controls a real operator. The TUI does not
perform workflow discovery, execute nodes, or manage Ray.

### CLI

The `ava` CLI is a thin front door:

- `ava operator` starts the local operator;
- `ava web` starts the browser listener and gRPC-Web proxy;
- `ava dev` starts both;
- `ava run` sends a run request, including JSON input/context and file or
  workspace attachments;
- `ava result` downloads a successful operator result;
- `ava tui` starts the optional terminal client;
- `ava webhooks list|get` inspects discovered local webhook routes.

## Ownership boundaries and invariants

| Boundary | Owner | Must not cross as a live object |
| --- | --- | --- |
| Workflow construction | author process | construction `ContextVar`, `NodeFuture` internals |
| Embedded execution | calling process | process-local `RunHandle` |
| Operator catalog | operator parent | live `Workflow` objects and imported modules |
| Operator run | spawned coordinator | workflow/executor instances and worker-local capabilities |
| Executor / Ray | executor and workers | Ray object references until materialization is required |
| UI state | browser/TUI client | operator state mutation and workflow execution |
| Result transport | result store + gRPC | arbitrary live Python values; results are encoded values + files |

These boundaries prevent accidental eager materialization, process-unsound
object sharing, and UI-owned execution.

## Repository map

| Area | Primary locations |
| --- | --- |
| Public DAG, runtime, storage, agent APIs | `src/avalanche/` |
| Local and Ray executors | `src/runtime/executor.py` |
| Operator lifecycle, registry, transport, worker | `src/runtime/operator/` |
| Browser UI source | `web/operator/` |
| Packaged browser assets | `src/runtime/operator/web_assets/` |
| Textual TUI | `src/tui/` |
| CLI | `src/ava_cli/` |
| Runnable examples | `examples/` |
| Behavior and integration tests | `test/` |

## Current limits

- Avalanche is intended for local development and experimentation.
- The operator retains bounded local state; it does not provide durable recovery
  across operator restarts.
- Loopback defaults and the browser trusted-proxy acknowledgement are not a
  production authentication model.
- Source discovery imports configured Python and therefore requires narrow,
  trusted flow paths.
- Live reload follows Python source changes only; restart for non-Python
  import-time configuration changes.
- Ray is optional and only participates when `RayExecutor` is selected.

## Related documents

- [README](README.md): installation, CLI use, providers, and examples.
- [DAG API](docs/dag-api.md): graph construction, inputs, runtime providers,
  reruns, and execution.
- [Agent steps](docs/agent-steps.md): typed model contracts and runtime
  configuration.
- [Data model and storage API](docs/data-model-api.md): namespaces, tables,
  schemas, and storage backends.
- [Execution services](docs/execution-services.md): executor-owned worker
  lifecycle services.
- [Examples](examples/README.md): runnable local workflows.
