# Architecture

Avalanche is a local-first system for defining typed Python workflow DAGs,
executing them locally, and controlling those runs through a
local operator. The system has explicit authoring, execution, control-plane,
and presentation boundaries. It does not provide production authentication,
multi-tenancy, durable recovery, or a deployment control plane. The local
operator keeps its run history, details, results and schedules in a local SQLite
file so they survive a restart, but it never resumes a run a restart interrupted
(see [Operator state](#operator-state)).

## System at a glance

```text
                         authoring and execution plane
┌──────────────────────────────────────────────────────────────────────────┐
│ Python workflow module                                                    │
│  @workflow + @source/@step/@agent_step/@dest                              │
│          │                                                                │
│          ├── embedded: Workflow.run() → RunHandle → LocalExecutor        │
│          │                                                                │
│          └── operator: discovery worker → descriptor catalog              │
└──────────┼───────────────────────────────────────────────────────────────┘
           │ serializable descriptors and source locations
           v
┌──────────────────────────────────────────────────────────────────────────┐
│ Local operator                                                           │
│  WorkflowRegistry · discovery cache · Python source watcher · scheduler   │
│  webhook ingress · gRPC server · SQLite state store (state.db)            │
│          │                                                                │
│          ├── spawn one coordinator process per run                        │
│          │       └── Workflow.run() → LocalExecutor                      │
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
operator-visible state and its storage, results, scheduling, webhook routing, and
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
does not forcibly interrupt an active local function.

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
uv run ava operator examples --port 7433

# Browser UI and REST are already available at http://127.0.0.1:7435.
# Optional terminal client, in another terminal:
uv run ava tui --connect localhost:7433
```

`ava operator` owns discovery, gRPC, and the loopback browser/REST listener;
`--no-web` retains gRPC-only operation. `ava dev examples` additionally opens the
browser automatically and waits for gRPC readiness before starting HTTP.
The browser UI and REST API listen on `http://127.0.0.1:7435` by default;
`--web-port` changes this port. Neither command starts the TUI.

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
the keyword-only `ava.Agent`, the step calls it, then the step body composes
and persists its own result. The step's signature, tools, and runtime settings
remain explicit declaration data.

An `AgentRuntime` runs one agent call. Avalanche hands it an `AgentRequest` (the
signature, validated inputs, tools, and Agent Skills); the runtime returns raw outputs plus a
runtime-neutral trace. Avalanche then validates the outputs against the
signature in one place, so every runtime shares the same output contract, and
returns an `AgentResult(outputs, trace)` to the step. PredictRLM is the default
runtime. Outside `avalanche.runtimes.predict_rlm`, PredictRLM is imported only
lazily: by the `DirectPythonBackend` re-export and by the TUI mock trace.

Skills follow the same split. `ava.agent.Skill` is a runtime-neutral Agent Skill
(name, description, instructions, resource files) passed as a step's `skills=`,
carried in the request and published in the shared part of the declaration. Each
runtime decides how to offer skills to its agent: ClaudeAgent writes them into a
temporary Claude Code plugin per call, OpenAIAgent lists them in the agent's
instructions and lets the model read them through two tools, and, when its SDK
sandbox has a shell, also writes them to a temporary folder so their scripts run,
PydanticAIAgent offers each as a Pydantic AI capability the model loads on demand,
and PredictRLM rejects them until it supports the format. PredictRLM's own skills, which bundle prompt text with sandbox
packages, modules and tools, stay a PredictRLM runtime setting under
`ava.runtimes.predict_rlm.skills`.

Each agent step runs on a runtime, a frozen descriptor such as
`ava.runtimes.PredictRLM(lm=..., sub_lm=...)`. A workflow-level
`agent_defaults={"runtime": ...}` supplies shared settings; a step's runtime of
the same kind is merged over it field by field, and with neither the step uses
`PredictRLM()`. Signatures and tools stay step-local.

While a runtime runs, it reports live events, and each call ends with exactly
one final event. A live event carries Avalanche's sequence number and a common
event (the run's start with its inputs and model, a finished turn, a tool call),
the runtime's own details as a typed native payload for its trace format, or
both; an event the common layer describes fully has no native part. The final
event carries the trace header, which every runtime fills the same way, and the
step's outputs, with files by reference and fields too large to keep named
rather than kept; the turns already travelled as live events. The complete
`AgentTrace`, header and turns, exists only in process, as `result.trace` and
for evaluations. The header and each turn carry a typed native part for their
runtime's trace format, and the runtime kind and trace format are read from that
part rather than stored again, so the run summary sent to clients holds runtime
details such as PredictRLM's sub-model and per-model usage without the trace
body. These models live in `src/avalanche/agent/` without runtime imports, so
the operator, client, TUI, and web UI validate every format without the runtime
installed. Only the bodies the operator stores and serves one by one, live
events and final events, carry a schema version.

### Executors

`LocalExecutor` runs dependency-ready nodes concurrently in a per-run thread
pool inside the local coordinator process. Its worker bound is configurable;
one worker uses synchronous serial admission, checking cancellation before each
node and leaving later ready siblings unqueued after cancellation or failure.
Node arguments and fan-in receipts retain DAG declaration order even when sibling
completion order differs. The DAG runtime, not the UI or operator
parent, controls dependency order and binding. The operator parent receives
lifecycle events and encoded terminal results rather than live task objects.

For an operator run, executor construction happens in the spawned coordinator:

```text
Operator parent
  └── coordinator imports one workflow module and builds one Workflow
        └── LocalExecutor: dependency-ready thread-pool nodes + queue observers
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

`WorkflowRegistry` receives resolved workflow targets. A file target is one
candidate; a directory target is recursively scanned for public `*.py`
files. Private files beginning with `_` and hidden/generated directories are skipped.
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
must be import-safe and side-effect-light. `ava operator` and `ava dev` use
explicit existing file or directory targets when supplied. Otherwise they read
`[tool.avalanche].flow_targets` from the nearest `pyproject.toml`; each relative
path is resolved from that file's directory. Do not configure a repository root
unless it is deliberately a flow-only tree:

```bash
# Avoid in a mixed repository:
uv run ava operator .

# Prefer an explicit specific module or flow directory:
uv run ava operator examples
```

An explicit target list replaces configured targets. If neither is present, the
command fails before starting services; it never falls back to the current directory.

### Live reload and scheduled metadata

The live watcher observes only non-excluded Python files below resolved import
roots. A changed workflow module or imported Python helper triggers targeted
discovery; adding or deleting a Python candidate updates the catalog without
reimporting unrelated candidates.

Any discovery failure is terminal for the local operator. A failed targeted scan
does not leave a stale catalog serving runs; startup or live reload exits with
the discovery diagnostic.

Live watching intentionally ignores non-Python changes. If a workflow reads an
import-time resource such as JSON to define a cron expression, restart the
operator after changing that resource. Startup cache validation is broader than
the live Python watcher, so a restart re-evaluates changed local source
metadata.

The scheduler reconciles discovered `cron` declarations with the catalog. It
starts runs through the same operator lifecycle as manual requests, but only for
schedules that are enabled. Schedules start disabled (or all enabled with
`--schedules-enabled`, which overrides stored choices for that start only);
enable/disable choices, the input each schedule's runs start with, and the minute
each last started a run are kept in the `schedules` table of the state database by
workflow ID, so they survive reconciliation, cron changes and restarts. The last
started minute is stored before the run starts, so a crash skips that occurrence
rather than starting it twice after a restart. The input is validated by the run worker, as for any other run. Workflow
descriptors can also declare webhook routes. When routes exist, the operator
starts a loopback HTTP ingress on port `7434` by default; valid JSON POST bodies
become workflow input and return an accepted run ID.

### Run coordination

The operator parent owns the authoritative local projection of each run:
`RunState`, per-node `NodeState`, logs, agent and classifier events, traces,
evaluations, update sequences, and a bounded structural baseline. Everything but
the baseline and the live stream buffers is written to the state database
([Operator state](#operator-state)). It launches each run using the `spawn`
multiprocessing context.

```text
1. Client calls StartRun.
2. Parent resolves a catalog descriptor to import root + module path + builder symbol.
3. Parent creates queue/events and a private result bundle, then spawns a coordinator.
4. Coordinator imports the module, builds the workflow, and sends preparation metadata.
5. Parent publishes REQUESTING then PENDING state and permits execution.
6. Coordinator runs Workflow.run(...).result() with lifecycle hooks.
7. Queue events update parent-side node/run state, logs, agent events, and traces.
8. Coordinator publishes an encoded terminal result into the private bundle.
9. Parent validates the bundle, stores a copy it owns, indexes it, and serves it.
```

Preparation, runtime, and terminal events are serializable queue messages. The
parent validates them before mutating its state. The coordinator forwards stdout,
stderr, logging, and agent evidence through the event channel. Agent evidence
crosses as JSON and is validated once, in strict mode, against the live event
and final event models. Malformed or out-of-order agent evidence, such as a
second final event for one call, or native parts from two runtimes in one call,
fails the run as a protocol error.

Cancellation sets the child cancellation event and eventually tears down the
owned process group. It remains cooperative at the workflow-node level, while
the operator owns final process cleanup.

### Results

A successful coordinator result is encoded into JSON plus separate `File`
attachments. The parent-owned `ResultStore` validates the worker's bundle
(manifest, digests and per-result size limits), writes a copy into a new
owner-only bundle under `results/` next to the operator state database
(`.avalanche/operator/results/` by default), and records it in the database's
`run_results` table once the run has succeeded. `GetRunResult` and
`ava result RUN_ID --output-dir PATH` serve it from there, re-validating it on
every read, so results survive an operator restart. Nothing expires by time.
With `--state-db :memory:` the bundles live in a private temporary folder that
is removed when the operator stops.

At startup the operator warns about bundles without an index row and index rows
without their bundle, and serves neither. The result folder is local and
unencrypted and can hold any value a workflow returns. Application-owned tables
and output destinations remain outside it.

### Operator state

`ava operator` and `ava dev` store everything they know in one SQLite file,
opened with Python's built-in `sqlite3` before discovery starts. `--state-db PATH`
chooses it; the default is `.avalanche/operator/state.db` under the folder the
command starts in, and `:memory:` gives a throwaway database (used by tests).
The code lives in `src/runtime/operator/state/`, one typed adapter module per
table. Rows are read back into the operator's typed models; a row that does not
validate is an error, never skipped.

```text
.avalanche/operator/
├── state.db          runs, steps, logs, events, traces, evaluations,
│                     result index, schedules, sequence counters
├── results/          accepted result bundles (owner-only)
├── input-files/      uploaded run input files; used ones are kept
└── schedule-inputs/  copies of files that schedule inputs use
```

What is stored:

- runs, with their step states and revisions and what they were started with
  (input, context and the input files they used);
- logs, agent events, classifier invocation snapshots, traces and evaluation
  records, written in the same transaction as the run publication that
  announces them, before any client is told about them;
- the result index (bundles stay as files in `results/`);
- each schedule's on/off state, input and last started minute;
- the update and catalog sequence counters.

Live-only state stays in memory: update-stream subscribers and their replay
buffer, structural baselines, active run process handles, and the workflow
registry, which discovery rebuilds on every start.

On a restart, including after a crash or `kill -9`:

- Sequence numbers continue from the stored counters and are never reused. Each
  start still gets a new `operator_instance_id`, so a client holding an older
  stream token is told to reload its baseline, and it reloads the stored history.
- Runs that were requesting, pending or running are **not resumed**. Before
  anything else can start a run, the operator stops any process group such a run
  left behind (recorded when the run started, with checks against process ID
  reuse and machine reboots), then marks the run and its unfinished steps failed
  with "Operator restarted before the run finished; it was not resumed". A trace
  still recording is closed with the same error. On Windows no process group is
  recorded: the run processes belong to the operator's kill-on-close Job Object
  and end with it.
- Evaluations still pending are marked failed with the same reason.
- Schedules come back as they were last turned on or off, with their input.
  `--schedules-enabled` turns all of them on for that start only and is not
  saved. A schedule's last started minute is written before its run starts, so a
  crash skips that occurrence rather than running it twice.

Startup refuses, with an error, a database already held by another operator, a
database written by a newer Avalanche version (schema versions are recorded in
`schema_migrations` and migrations only move forward), and a stored schedule
whose input file is missing (restore it or run
`ava state clear --include-schedules`). Result bundles that disagree with the
index are warnings, as described under [Results](#results).

One operator owns a database at a time. The database is opened in SQLite's
exclusive locking mode, so the lock comes from SQLite's own file locking and is
released by the OS when the process dies; a second operator, or an `ava state`
command, fails at once with an error naming the file. Locks are unreliable on
network filesystems, so keep the file on a local disk. The folders next to the
database belong to it alone; two databases in one folder are refused through
the `results/` lock. CI exercises the lock on Linux only.

Nothing is deleted by time or size. Per-item limits that protect transport
(bytes per log, agent event, trace body and run detail, and per-result limits)
still apply. `ava state info`, `delete`, `prune` and `clear` (see
[CLI reference](docs/api/cli.md#ava-state-command-options---state-db-path)) are
the only way to remove data; they run only while no operator holds the database.
`delete` refuses a run that did not finish before its operator stopped; `clear`
stops such a run's leftover processes first and also removes leftover files.

The database and the folders are local and **unencrypted**. They hold step
inputs, tool outputs, logs, traces and results exactly as they were recorded.

## Transport and clients

### gRPC operator contract

`src/runtime/operator/proto/operator.proto` is the transport boundary. The
operator exposes a single native service, `OperatorServiceV2`:

- `DiscoverFlows` for workflows, topology, scan targets, and diagnostics;
- `StartRun`, `CancelRun`, and `GetRunResult` for control and terminal output;
- `StageInputFile` to upload a run input file before `StartRun` or
  `SetScheduleEnabled` refers to it;
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
its idempotency key. Input files arrive as `input_files` attachments: a local
operator accepts inline bodies and `object_key` values that its own
`StageInputFile` issued, and rejects object URIs and any other key. Each
attachment names a field the input schema declares; a list field collects its
attachments in order, and any other field takes exactly one. Staged files
live in `input-files/<token>/<name>` next to the state database; each run gets an
`ava.File` built with `File.from_path` over the staged copy. Files no run or
schedule used expire after 24 hours and are removed when the operator stops;
files a run used stay with that run until `ava state` deletes it. With
`--state-db :memory:` the folder is temporary and removed when the operator
stops. Files a schedule input uses are copied into `schedule-inputs/<token>/<name>`
next to the state database, each with its own host-local workspace reference that
the operator registers again at startup. Once a new schedule input no longer refers
to a copy, it is removed when the operator closes, after its runs have stopped.
Schedule input that refers to any other `ava.File` is refused.

The default ports are:

| Endpoint | Default | Purpose |
| --- | ---: | --- |
| gRPC operator | `7433` | discovery, run control, snapshots, updates, results |
| webhook ingress | `7434` | loopback JSON webhook routes when declared |
| browser UI / REST | `7435` | static UI, gRPC-Web proxy, and `/api/v1` JSON API |

### Browser UI

The browser UI source lives in `web/operator/` and is built with Vite, React,
and generated TypeScript protobuf bindings. Packaged static assets live in
`src/runtime/operator/web_assets/`.

The built-in loopback Uvicorn server hosts the static assets, a FastAPI REST
application mounted at `/api`, and the gRPC-Web proxy to the native operator.
`ava web` can also launch it
separately for a configured operator. The browser does not receive a direct
Python or gRPC channel to user workflow code.

`src/runtime/operator/rest.py` handles `/api/v1` discovery, enabling and disabling
schedules, run creation, summaries, snapshots, cancellation, output, and activity. Requests are validated
at the HTTP boundary and translated to the same unary `OperatorServiceV2` RPCs.
FastAPI supplies route declarations, request schemas, and OpenAPI documentation;
blocking RPC calls run in worker threads rather than on the HTTP event loop.
The listener owns the shared gRPC channel. Browser disconnects cancel upstream
stream calls, and shutdown closes the channel before draining HTTP requests.
Responses use snake_case protobuf JSON, including encoded result documents and
scope-bound page continuations. This adds no execution state, deployment
infrastructure, or durable command queue. REST shares the listener's local-trust
model, not an independent authentication layer. See README for the HTTP contract.

```text
Browser SPA
  ├── DiscoverFlows + paginated live run-summary baseline
  ├── WatchRunStatus from the current cursor
  ├── fetch selected snapshots, logs, agent events, and details on demand
  └── StartRun / CancelRun actions
          │ gRPC-Web
          ▼
ava web listener ── native gRPC ── OperatorServiceV2
```

Run-summary pages keep a stable source identity while their observed head moves
forward. A continuation repeats the exact observation that issued it, so a client
can follow a live page chain without crossing sources or accepting a head rewind.

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
perform workflow discovery or execute nodes.

### CLI

The `ava` CLI is a thin front door:

- `ava operator` starts the local operator and HTTP listener (`--no-web` disables HTTP);
- `ava web` starts a separate browser, REST, and gRPC-Web listener;
- `ava dev` starts the operator and HTTP listener, then opens the browser;
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
| UI state | browser/TUI client | operator state mutation and workflow execution |
| Result transport | result store + gRPC | arbitrary live Python values; results are encoded values + files |

These boundaries prevent accidental eager materialization, process-unsound
object sharing, and UI-owned execution.

## Repository map

| Area | Primary locations |
| --- | --- |
| Public DAG, runtime, storage, agent APIs | `src/avalanche/` |
| Local executor | `src/runtime/executor.py` |
| Operator lifecycle, registry, transport, worker | `src/runtime/operator/` |
| Browser UI source | `web/operator/` |
| Packaged browser assets | `src/runtime/operator/web_assets/` |
| Textual TUI | `src/tui/` |
| Agent trace viewers, one per trace format | `src/tui/agent_trace_viewers/`, `web/operator/src/agentTrace/` |
| CLI | `src/ava_cli/` |
| Runnable examples | `examples/` |
| Behavior and integration tests | `test/` |

## Current limits

- Avalanche is intended for local development and experimentation.
- The operator keeps run history, details, results and schedules in a local,
  unencrypted SQLite file, and nothing expires automatically. It does not provide
  durable recovery: a run interrupted by a restart is failed, never resumed.
- One operator per database file; there is no shared, multi-operator or remote
  store, and no backup or replication.
- The result store, and so the operator and `ava state`, refuse Windows with
  "Secure workflow result storage is unavailable", as before this storage
  existed. Database locking on macOS and Windows relies on SQLite and is not
  covered by CI.
- Loopback defaults and the browser trusted-proxy acknowledgement are not a
  production authentication model.
- Source discovery imports configured Python and therefore requires narrow,
  trusted flow paths.
- Live reload follows Python source changes only; restart for non-Python
  import-time configuration changes.
- `LocalExecutor` is the only in-repository executor.

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
