# Architecture

This document describes the implemented Avalanche/Avalanche architecture in this
repository.

## Bottom line

Avalanche is the terminal UI and monitoring/control surface for Avalanche flows.
It does not execute work directly. In production mode, Avalanche talks to an
operator daemon over gRPC; the operator discovers flows, manages runs, and
executes work through a pluggable executor such as `LocalExecutor` or
`RayExecutor`.

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

Avalanche is the frontend. The runtime is the optional backend component: the
operator is its control plane, and the executor is its execution plane.

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
- `src/avalanche/tui/`
- `src/runtime/operator/client.py`
- `src/avalanche/operator/client.py`

`src/runtime/operator/client.py` is listed here because `GrpcStateProvider` is
operator-client code used by the TUI. The implementation lives in `src/tui`;
`src/avalanche/tui` is a thin compatibility shim for `python -m avalanche.tui`,
and `src/avalanche/operator` is a thin compatibility shim over `src/runtime/operator`.
Both shims raise install-extra messages if the optional component is unavailable.

## Operator

```text
Operator
├── CLI entrypoint
│   └── parses --flows, --port, --ray
├── gRPC service
│   ├── ListWorkflows
│   ├── StartRun
│   ├── CancelRun
│   ├── ListRuns
│   ├── GetRun
│   └── StreamUpdates
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

For each scanned Python file, the registry:

1. builds a temporary import name from the file stem;
2. removes any cached module with that temporary name;
3. imports the file with `importlib.util.spec_from_file_location(...)`;
4. ignores the file if import raises an exception;
5. iterates over public module attributes;
6. calls public zero-argument callables;
7. keeps results that are instances of `Workflow`;
8. converts each discovered workflow into a `WorkflowInfo` snapshot;
9. stores the original callable as the builder used for future runs.

This means discovered flow functions must be safe to import and safe to call
with no arguments. The callable should build and return a `Workflow` object; it
should not perform expensive or irreversible work during discovery.

Because directory scanning imports arbitrary Python files, avoid pointing
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
- `src/avalanche/operator/`

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

The operator connects to Ray, but Avalanche does not.

### Executor key files

- `src/runtime/executor.py`
- `src/avalanche/executor.py`
- `src/avalanche/dag.py`
- `src/avalanche/runtime/`
- `src/runtime/operator/hooks.py`

## Cross-component boundaries

### State provider boundary

The TUI depends on the `StateProvider` protocol rather than on operator internals.
The protocol is intentionally small:

```python
list_workflows() -> list[WorkflowInfo]
list_runs(workflow_name: str) -> list[RunState]
get_run(run_id: str) -> RunState | None
start_run(workflow_name: str) -> str
cancel_run(run_id: str) -> None
on_run_update(callback) -> None
on_log(callback) -> None
```

This boundary allows the same TUI to run against:

- the mock provider for local UI work;
- the gRPC operator for real workflow control;
- future providers, if needed.

### Data model boundary

`WorkflowInfo` is the serializable snapshot used by Avalanche:

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
- `workflow_name`
- `status`
- `started_at` / `ended_at`
- per-node `NodeState`
- `LogEntry` records
- `triggered_by`

The UI does not hold or mutate real DAG objects. It renders these snapshots and
sends control requests back through the provider.

### gRPC boundary

The operator exposes these main RPCs through `OperatorService`:

- `ListWorkflows` returns discovered workflows.
- `StartRun` starts a new workflow run.
- `CancelRun` requests cancellation for a run.
- `ListRuns` returns runs for a workflow.
- `GetRun` returns one run by ID.
- `StreamUpdates` server-streams run-state updates to connected clients.

The default operator port is `7433`.

## Runtime modes

### Mock UI mode

Use this for UI development and visual exploration. No operator, gRPC server, or
Ray cluster is required.

```bash
uv run python -m avalanche.tui
```

This mode uses `MockStateProvider` and hardcoded demo workflows such as
`order_workflow`, `analytics_workflow`, `ml_workflow`, `data_platform`, and
`doc_processing`.

### Real operator mode with local execution

Use this when you want Avalanche to control real discovered flows without
Ray.

```bash
uv run python -m avalanche.operator \
  --flows examples \
  --port 7433
```

In another terminal:

```bash
uv run python -m avalanche.tui --connect localhost:7433
```

### Real operator mode with Ray execution

Use this when the operator should submit flow tasks to a Ray cluster.

```bash
uv run ray start --head --node-ip-address=127.0.0.1 --port=6379
```

```bash
RAY_ADDRESS=127.0.0.1:6379 uv run python -m avalanche.operator \
  --flows examples \
  --port 7433 \
  --ray
```

In another terminal:

```bash
uv run python -m avalanche.tui --connect localhost:7433
```

If a Ray head is already running on `127.0.0.1:6379`, skip `ray start` and point
`RAY_ADDRESS` at the existing cluster.

## Control flow

### TUI startup

1. `python -m avalanche.tui` calls `launch_tui()`.
2. Without `--connect`, the app uses the mock provider.
3. With `--connect HOST:PORT`, the app creates a `GrpcStateProvider`.
4. `AvalancheApp` initializes `UIStore` from the selected provider.
5. Widgets render state from `UIStore`.

### Operator startup

1. `python -m avalanche.operator` parses `--flows`, `--port`, and `--ray`.
2. Without `--ray`, the operator uses `LocalExecutor`.
3. With `--ray`, the operator uses `RayExecutor`.
4. `WorkflowRegistry` scans the configured flow files/directories.
5. The gRPC server exposes the operator on the configured port.
6. The file watcher and scheduler run in background threads when flow paths
   are configured.

### Starting a run

1. The user triggers a run in Avalanche.
2. The TUI calls `StateProvider.start_run(workflow_name)`.
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

The operator discovers flows through `WorkflowRegistry.scan(paths)`. For each
configured `.py` file, it imports the module and looks for public zero-argument
callables that return a `Workflow` object.

Avoid scanning the repository root directly:

```bash
# Avoid from repo root: imports too many arbitrary Python files.
uv run python -m avalanche.operator --flows .
```

Prefer a specific flow file or a clean flow-only directory:

```bash
uv run python -m avalanche.operator \
  --flows examples \
  --port 7433
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

The effective production path is:

```text
Avalanche TUI → GrpcStateProvider → Operator → RayExecutor → Ray cluster
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

- `spec/TUI.md` — original Avalanche/TUI design notes.
- `spec/decisions.md` — broader Avalanche architecture decisions.
- `spec/IMPLEMENTATION_SUMMARY.md` — historical implementation summary.
- `doc/dag-api.md` — user-facing DAG API notes.
