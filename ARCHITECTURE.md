# Architecture

This document describes the implemented Avalanche architecture in this repository.

## Bottom line

Avalanche combines a Python flow runtime, an operator daemon, and a terminal UI.
The TUI does not execute work directly. In operator-connected mode, the TUI talks
to the operator over gRPC; the operator discovers flows, manages runs, and
executes work through a pluggable executor such as `LocalExecutor` or
`RayExecutor`.

## Local development modes

Avalanche exposes the same workflow runtime through two local development modes.

### Embedded mode

```text
Python flow program -> Workflow.run() -> RunHandle -> LocalExecutor or RayExecutor
```

The program that declares the flow builds it and calls `Workflow.run()` directly.
The call synchronously allocates the run ID and returns a process-local
`RunHandle`; one named non-daemon driver thread executes the existing blocking
driver. The program explicitly waits with `.result()` or `await`. Handle
cancellation is cooperative between node submissions and does not interrupt an
active thread or Ray task. No operator, gRPC service, CLI client, or connected
TUI participates.

```bash
uv run python examples/operator_workflow.py
```

### Operator-managed mode

```text
CLI or TUI -> gRPC -> Operator -> Workflow.run().result() -> LocalExecutor or RayExecutor
```

The operator runs separately, imports and discovers configured flow files, and
owns run state, logs, cancellation, schedules, and file watching. The `ava run`
command and connected TUI are control-plane clients. The TUI never executes the
flow itself.

```bash
# Terminal 1
uv run ava operator --flows examples --port 7433

# Terminal 2: choose the CLI or TUI
uv run ava run operator_demo_workflow --connect localhost:7433
uv run ava tui --connect localhost:7433
```

`ava dev --flows examples` is a convenience form of operator-managed mode. It
starts an operator subprocess and a connected TUI together. The operator still
executes each discovered flow by calling the same `Workflow.run()` path used in
embedded mode and explicitly waits on its handle. Operator state, logs, and
control remain the externally managed run projection; the embedded handle is
not a durable registry, persisted result, or recovery mechanism.

## High-level component model

```text
TUI <> Runtime

┌──────────────────────────────┐
│ TUI                          │
│ - AvalancheApp               │
│ - UIStore                    │
│ - screens/widgets            │
│ - StateProvider              │
│ - mock/gRPC providers        │
│ - shared state model imports │
└───────────────┬──────────────┘
                ⇅ state API / gRPC
                ⇅ start, cancel, list, stream
┌───────────────┴──────────────┐
│ Runtime                      │
│ ┌──────────────────────────┐ │
│ │ Operator                 │ │
│ │ - CLI entrypoint         │ │
│ │ - gRPC service           │ │
│ │ - WorkflowRegistry       │ │
│ │ - scheduler / watcher    │ │
│ │ - run state / log capture│ │
│ └────────────┬─────────────┘ │
│              ⇅ executor API  │
│              ⇅ submit/get    │
│ ┌────────────┴─────────────┐ │
│ │ Executor ∈ {Local, Ray}  │ │
│ │ - Ray when selected      │ │
│ │ - DAG runtime hooks      │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
```

The TUI is the frontend. The operator is the control plane, and the executor is
the execution plane.

## TUI

```text
TUI
├── AvalancheApp
│   └── Textual application lifecycle
├── UIStore
│   └── current flow, run, selection, sidebar, search, cache
├── screens/
│   ├── flow detail
│   └── run detail
├── widgets/
│   ├── DAG view
│   ├── sidebar
│   ├── run history
│   ├── log panel
│   ├── schedule panel
│   └── status/title bars
├── StateProvider protocol
│   ├── MockStateProvider
│   └── GrpcStateProvider
└── shared state model imports
    ├── WorkflowInfo
    ├── RunState
    ├── NodeState
    └── LogEntry
```

### TUI responsibilities

The TUI provides the human control and observability surface.

It owns:

- terminal rendering;
- flow and run navigation;
- DAG visualization;
- start/cancel user actions;
- log and run-history display;
- local UI state such as selection, focus, and search.

It does not own:

- real flow discovery;
- real run execution;
- Ray cluster management.

### TUI key files

- `src/tui/__main__.py`
- `src/tui/app.py`
- `src/tui/ui_store.py`
- `src/tui/state.py`
- `src/tui/models.py`
- `src/tui/mock.py`
- `src/tui/screens/`
- `src/tui/widgets/`
- `src/runtime/operator/client.py`

`src/runtime/operator` contains both the operator client and control plane;
`src/tui` contains the Textual client implementation.

## Operator

```text
Operator
├── CLI entrypoint
│   └── parses --flows, --port, --ray
├── gRPC service
│   ├── ListFlows / StartRun / CancelRun
│   ├── GetRunResult
│   ├── ListRunSummaries / GetRunSnapshot
│   ├── ListLogs / ListAgentEvents
│   ├── ReadTrace / ReadDetail
│   └── StreamRunUpdates
├── Operator core
│   ├── run lifecycle
│   ├── node state tracking
│   ├── cancellation
│   ├── subscriber updates
│   └── log capture
├── WorkflowRegistry
│   ├── file/directory scan
│   ├── module import
│   ├── Workflow detection
│   └── WorkflowInfo snapshots
├── scheduler
│   └── cron-triggered runs
├── file watcher
│   └── rescan changed flow files
└── conversion/proto layer
    └── Python models ↔ protobuf messages
```

### Operator responsibilities

The operator acts as the backend control plane for real flows.

It owns:

- scanning configured flow files/directories;
- converting discovered DAGs into `WorkflowInfo` snapshots;
- accepting start/cancel requests;
- creating and tracking `RunState` and `NodeState`;
- running flows in background threads;
- attaching execution hooks;
- capturing logs;
- broadcasting run updates;
- watching flow files for changes;
- triggering cron-scheduled runs.

It does not own:

- terminal rendering;
- user navigation state;
- the internals of Ray cluster scheduling.

### Flow scanning details

Flow scanning is owned by `WorkflowRegistry`, which is created by the
operator at startup. The operator passes the configured `--flows` paths into
`WorkflowRegistry.scan(paths)`.

For each configured path:

- if the path is a `.py` file, the registry scans that file;
- if the path is a directory, the registry recursively scans `*.py` files;
- private files whose names start with `_` are skipped during directory scans.

For each scanned Python file, a short-lived discovery process:

1. imports the file using its normal package path when available;
2. finds public functions marked by `@workflow`;
3. calls those builders to construct their DAGs;
4. validates schedules and converts each DAG into a serializable descriptor;
5. records the local Python files imported while discovering that candidate.

The operator caches those descriptors and dependency paths under
`.avalanche/cache/operator/`. A later startup reuses the cache when the Python
environment and watched source-file metadata are unchanged. Hot reload uses the
recorded imports to rediscover only changed workflow files and workflows that
depend on a changed Python helper. Adding or deleting a Python file updates only
that candidate. A non-Python source change triggers a complete rescan because
ordinary Python code can read such resources without declaring them.

The cache contains metadata only, never live `Workflow` objects or executable
callables. Discovery still runs in an isolated process whenever source must be
evaluated. Workflow modules and builders must therefore be safe to import and
call, and should not perform expensive or irreversible discovery-time work.

Because the first directory scan imports arbitrary Python files, avoid pointing
`--flows` at the repository root. Prefer a specific flow file or a clean
flow-only directory.

### Operator key files

- `src/runtime/operator/__main__.py`
- `src/runtime/operator/operator.py`
- `src/runtime/operator/server.py`
- `src/runtime/operator/models.py`
- `src/runtime/operator/client.py`
- `src/runtime/operator/registry.py`
- `src/runtime/operator/scheduler.py`
- `src/runtime/operator/hooks.py`
- `src/runtime/operator/convert.py`
- `src/runtime/operator/proto/`

## Executor

```text
Executor ∈ {LocalExecutor, RayExecutor}
├── interface
│   ├── submit(fn, *args, **kwargs)
│   ├── get(futures)
│   └── shutdown()
├── Ray cluster, only when RayExecutor is selected
│   ├── head node
│   ├── workers
│   └── Ray dashboard
└── DAG runtime integration
    ├── dependency-aware task execution
    └── operator hooks for status/log updates
```

### Executor responsibilities

The executor owns the execution strategy for a workflow run.

It owns:

- submitting node functions;
- materializing task results;
- respecting DAG dependencies through the workflow runtime;
- providing backend-specific execution behavior.

Current implementations:

- `LocalExecutor` for in-process execution;
- `RayExecutor` for Ray-backed distributed execution.

### Ray responsibility boundary

Ray provides distributed compute when `RayExecutor` is selected.

Ray owns:

- Ray workers and object references;
- remote task scheduling;
- distributed result materialization;
- Ray dashboard and cluster lifecycle, outside Avalanche's CLI.

The operator connects to Ray; the TUI does not.

### Executor key files

- `src/runtime/executor.py`
- `src/avalanche/dag.py`
- `src/avalanche/runtime/`
- `src/runtime/operator/hooks.py`

## Execution services

Execution services are an executor-owned worker lifecycle for platform-provided
resources such as immutable input snapshots, task-local filesystems, output
reservations, and commit receipts. They are separate from runtime providers such
as `ava.Stream`: runtime providers inject task arguments from Avalanche-owned
data systems, while execution services surround the entire user task.

```text
Workflow.run(execution_services=spec)
  -> executor submits one service-managed task
     -> probe -> negotiate -> open
     -> materialize_input
     -> user task
     -> finalize -> receipt
     -> teardown
```

The service request is immutable executor metadata. It must not carry credentials,
user-facing storage URIs, absolute worker paths, open handles, actors, or affinity
tokens. The provider acquires worker-local capabilities after scheduling. Input
materialization may be eager or lazy; for example, a provider can return paths in
an attempt-local filesystem whose bytes are fetched on first access.

Local execution carries values directly. Ray execution returns user payloads, small
service receipts, and status markers through separate object-reference channels. The
driver observes statuses, while parent receipts remain worker-side dependencies of
downstream service sessions. Fan-in follows the DAG without fetching intermediate
payloads or receipts on the driver. `RunHandle` exposes only deterministically ordered
terminal receipts.

Any failure after `open` requests abort and then tears the session down exactly once.
Cleanup errors are attached as notes instead of masking the primary failure. Providers
may preserve explicit recovery state when destructive cleanup is unsafe. If an executor
retries a task, the whole worker lifecycle starts again; Avalanche does not reuse an
opened session. There is no fallback to ordinary execution when service negotiation or
materialization fails.

See [docs/execution-services.md](docs/execution-services.md) for the public
protocol, provider contract, and workflow-author experience.

## Cross-component boundaries

### State provider boundary

The TUI depends on the `StateProvider` protocol rather than on operator internals.
The protocol is intentionally small:

```python
list_workflows() -> list[WorkflowInfo]
list_runs(flow_name: str) -> list[RunState]
get_run(run_id: str) -> RunState | None
start_run(flow_name: str) -> str
cancel_run(run_id: str) -> None
on_run_update(callback) -> None
on_log(callback) -> None
```

This boundary allows the same TUI to run against:

- the mock provider for local UI work;
- the gRPC operator for real workflow control;
- future providers, if needed.

### Data model boundary

`WorkflowInfo` is the serializable snapshot used by the TUI:

- `name`
- `file_path`
- `node_ids`
- `graph`
- `node_types`
- `display_names`
- `cron`
- schedule metadata such as `next_run_at` and `last_run_at`

`RunState` captures live execution state:

- `run_id`
- `flow_name`
- `status`
- `started_at` / `ended_at`
- per-node `NodeState`
- `LogEntry` records
- `triggered_by`

The UI does not hold or mutate real DAG objects. It builds lightweight run state
from paginated structural snapshots and typed updates. Logs, agent events, traces,
and encoded detail bodies are fetched through bounded, on-demand RPCs and merged
only for the selected run. Control requests travel back through the provider.

### gRPC boundary

The operator exposes these main RPCs through `OperatorService`:

- `GetCatalog` returns discovered workflows and scan-target metadata.
- `StartRun` starts a new workflow run; caller-owned IDs are limited to 256
  UTF-8 bytes so retained summaries stay bounded.
- `CancelRun` requests cancellation for a run.
- `GetRunResult` returns the encoded result for a successful run.
- `ListRunSummaries` pages lightweight runs from a retained structural baseline.
- `GetRunSnapshot` returns one structural run snapshot pinned to an operator
  instance and sequence.
- `ListLogs` and `ListAgentEvents` page snapshot-pinned detail descriptors.
  Agent event descriptors carry both the invocation identity and the
  operator-assigned retention cursor; source-local event sequences remain in
  the fetched detail body.
- `ReadTrace` and `ReadDetail` stream bounded chunks for immutable detail bodies.
- `StreamOperatorUpdates` replays typed changes under an operator-instance epoch.
  Stale cursors, restarts, and slow-consumer overflow require an explicit
  structural reset.

This protocol replaces the previous `ListRuns`, `GetRun`, and `StreamUpdates`
full-state RPCs. It is a breaking wire change: clients and servers must use
protobuf bindings generated from the same transport contract.

The default operator port is `7433`.

## Runtime modes

### Mock UI mode

Use this for UI development and visual exploration. No operator, gRPC server, or
Ray cluster is required.

```bash
uv run ava tui
```

This mode uses `MockStateProvider` and hardcoded demo workflows such as
`order_workflow`, `analytics_workflow`, `ml_workflow`, `data_platform`, and
`doc_processing`.

### Real operator mode with local execution

Use this when you want the operator and TUI to control real discovered flows
without Ray.

```bash
uv run ava operator --flows examples --port 7433
```

In another terminal:

```bash
uv run ava tui --connect localhost:7433
```

### Real operator mode with Ray execution

Use this when the operator should submit flow tasks to a Ray cluster.

```bash
uv run ray start --head --node-ip-address=127.0.0.1 --port=6379
```

```bash
RAY_ADDRESS=127.0.0.1:6379 uv run ava operator --flows examples --port 7433 --ray
```

In another terminal:

```bash
uv run ava tui --connect localhost:7433
```

If a Ray head is already running on `127.0.0.1:6379`, skip `ray start` and point
`RAY_ADDRESS` at the existing cluster.

## Control flow

### TUI startup

1. `ava tui` calls `launch_tui()`.
2. Without `--connect`, the app uses the mock provider.
3. With `--connect HOST:PORT`, the app creates a `GrpcStateProvider`.
4. `AvalancheApp` initializes `UIStore` from the selected provider.
5. Widgets render state from `UIStore`.

### Operator startup

1. `ava operator` parses `--flows`, `--port`, and `--ray`.
2. Without `--ray`, the operator uses `LocalExecutor`.
3. With `--ray`, the operator uses `RayExecutor`.
4. `WorkflowRegistry` scans the configured flow files/directories.
5. The gRPC server exposes the operator on the configured port.
6. The file watcher and scheduler run in background threads when flow paths
   are configured.

### Starting a run

1. The user triggers a run in the TUI.
2. The TUI calls `StateProvider.start_run(flow_name)`.
3. In real mode, `GrpcStateProvider` sends `StartRun` to the operator.
4. The operator looks up the workflow builder in `WorkflowRegistry`.
5. The operator creates a `RunState` and `NodeState` entries for each node.
6. Execution starts in a background thread.
7. The selected executor runs DAG tasks.
8. Hooks mark node start/success/failure and capture logs.
9. The operator broadcasts run updates to subscribers.
10. The TUI receives updates and refreshes the visible DAG, logs, and run
    history.

## Flow discovery

The operator discovers flows through `WorkflowRegistry.scan(paths)`. It imports
configured Python candidates in an isolated process and invokes functions marked
by `@workflow`. Unchanged descriptors are subsequently reused from the local
discovery cache.

Avoid scanning the repository root directly:

```bash
# Avoid from repo root: imports too many arbitrary Python files.
uv run ava operator --flows .
```

Prefer a specific flow file or a clean flow-only directory:

```bash
uv run ava operator --flows examples --port 7433
```

## Execution and Ray

Ray is part of the execution plane, not the TUI plane.

- Avalanche never calls Ray directly.
- The operator creates an executor for each run.
- `LocalExecutor` runs work locally.
- `RayExecutor` wraps functions with `ray.remote(...)` when needed and resolves
  results with `ray.get(...)`.
- For Ray runs, operator log capture uses `ray.util.queue` so worker logs can be
  streamed back into the run state.

The local operator-connected path is:

```text
TUI -> GrpcStateProvider -> Operator -> RayExecutor -> Ray cluster
```

## Current caveats

- `--flows .` from the repository root is unsafe because discovery imports
  arbitrary Python files; use a specific file or clean flow directory.
- The `--ray` CLI flag selects `RayExecutor`, but Ray connection details are not
  exposed as first-class CLI flags. Use `RAY_ADDRESS=...` to connect to an
  existing Ray cluster.
- The mock provider contains richer demo workflows than the currently
  discoverable real workflow fixtures.
- `WorkflowInfo` is a UI snapshot; backend-only details on the live `Workflow`
  object are intentionally not exposed to the TUI.

## Related documents

- `README.md` for the shortest onboarding path.
- `docs/getting-started.md` for the public getting-started guide.
- `examples/README.md` for canonical runnable examples.
