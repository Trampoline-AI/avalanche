# Changelog

## Unreleased

## 0.1.2
### Dependencies

- Declare Protobuf as an Avalanche runtime dependency required by the operator's
  generated gRPC bindings.

## 0.1.1

### Workspace initialization

- `ava init` now rebuilds its virtual environment after moving a staged workspace
  into its final location, so generated `ava` entry points use the final path.

### Dependencies

- Avalanche now installs PredictRLM's `codex-lm` extra by default, enabling Codex
  LM support.

## 0.1.0

### Operator web interface

- Added an opt-in local React operator interface (`ava web`) with workflow
  discovery, live DAG replacement, immutable historical run canvases,
  launch/cancel controls, logs, and demand-loaded agent evidence.
- Added a browser listener and packaged browser assets. Loopback remains the
  default; non-loopback binding requires the explicit `--trusted-proxy`
  acknowledgement.
- Run topology now retains only versioned agent input/output field schemas,
  while bounded trace descriptors expose stable PredictRLM header, usage, and
  telemetry metadata without embedding declaration instructions or complete
  trace bodies in structural snapshots.
- Unchanged discovery results no longer advance catalog revisions, and the web UI
  retains workflow/run navigation on narrow viewports with accessible input and
  repeated-node labels plus WCAG AA secondary-text contrast.
- Workflow cards now show contained typed field lists while declarations remain in the inspector.
  Canvases retain depth-aware edge routing, live durations, stronger execution states, and `Run`
  labels.
- Agent steps now use an explicit DAG-card label and accent, including historical
  runs classified from their retained agent field schemas.
- Successful and failed nodes retain their neutral borders; only their titles
  and status labels use the corresponding outcome color.
- The current workflow canvas uses React Flow's neutral dotted blueprint field;
  historical run canvases retain their separate neutral presentation.
- Run logs now render bounded ANSI SGR color and text-style sequences without
  interpreting log content as HTML.
- Node-scoped run logs now retain canonical node IDs, including repeated-node
  suffixes, so selecting a log node preserves its filtered records.
- Starting a workflow now navigates directly to its retained run snapshot as
  soon as the operator publishes the run ID.
- Agent steps now default PredictRLM to quiet execution; workflows and
  individual steps can explicitly opt into verbose trace logs.
- Zoomed-out run nodes center their titles while preserving a larger,
  card-corner duration label.
- Failed DAG cards now show status only; inspect their retained logs for error detail.
- Retained run canvases now include a `Current workflow` control that returns
  directly to the live workflow view.
- Explorer collapse and restore controls now stay at the pane edge, and Explorer and inspector
  panes are independently resizable. Retained inputs, outputs, and traces use bounded progressive
  JSON with content-sized key columns, while logs use a record-separated continuous-text view
  without hidden unbounded DOM.
- Large-run hydration is now summary-first, cancellable, incrementally paged, and
  bounded across browser queues, descriptor windows, detail caches, and virtualized
  DOM rendering. `make web-bench` covers 10,000 retained runs in real Chromium.
- Added `ava operator --log-level` and explicit source-watcher and hot-reload
  lifecycle logs for successful, unchanged, and failed catalog refreshes.

### Operator transport

- Operator streams now replay bounded, typed run updates under an instance epoch
  and explicitly require a structural reset for stale cursors or restarts.
- Remote TUI state applies updates in sequence, ignores duplicates, and reloads
  structural run baselines instead of receiving complete run snapshots per event.
- Slow consumers receive an explicit reset from bounded stream queues, while
  summary refreshes preserve already-hydrated run details.
- Run selection paginates historical logs and agent events on demand. Trace
  hydration uses one lifecycle-owned worker and epoch/revision-guarded detail
  completions, with bounded backoff.
- The transport protobuf is not backward compatible with the previous full-state
  RPCs. Operators and remote TUI clients must upgrade together and regenerate
  bindings from the same protocol revision.
- Operator detail retention now enforces per-event, per-trace, per-node,
  per-run log count/byte, and aggregate per-run limits before accepting payloads.
- The TUI coalesces its cross-thread provider handoff in a bounded queue and
  schedules deterministic snapshot repair if sustained pressure drops detail.
- Caller-owned run IDs are limited to 256 UTF-8 bytes.
- Agent detail events retain a per-invocation source sequence and a separate
  transport cursor, so repeated calls to the same agent node do not discard
  later evidence when the source sequence restarts at one.

### TUI performance

- Virtualized log rendering keeps steady and incremental refresh work bounded by
  the visible viewport instead of rebuilding complete log history every frame.
- DAG pointer scrolling now accumulates higher-sensitivity horizontal and
  vertical targets with short animations instead of jumping between cells.
- Added `make tui-bench`, which enforces the 30 FPS refresh budget through
  10,000 log rows for steady and append scenarios.

### Worker execution services

- Added the versioned `ava.ExecutionServicesSpec` and worker-side
  `probe -> negotiate -> open -> materialize_input -> finalize/abort -> teardown`
  lifecycle for platform-managed task resources.
- Local and Ray executors carry service receipts separately from user payloads;
  terminal receipts are available through `RunHandle.execution_receipts()`.
- Input materialization happens in the consuming worker and may be eager or lazy.
  Failures do not silently fall back to ordinary execution.
- See [docs/execution-services.md](docs/execution-services.md).

### Awaitable workflow run handles

- Breaking: `Workflow.run(...)` now immediately returns a generic, awaitable
  `ava.RunHandle` instead of the workflow output. Use `.result()` to block or
  `await` the handle in asynchronous code.
- Run handles expose the synchronously allocated `run_id`, cached terminal
  output or failure, timeout-aware result access, and cooperative cancellation.
- Each embedded run uses one named non-daemon driver thread. Cancelling an
  asyncio waiter does not cancel the run, and active Python or Ray work is not
  forcibly interrupted.
- Handles are process-local lifecycle objects. Durable operator status,
  registries, persisted outputs, and recovery contracts are unchanged.

### Native agent support: bodyful `@ava.agent_step` and typed tables

- Pydantic `BaseModel` classes are now a first-class table schema source for
  `ava.IcebergTable(schema=...)` and `ava.LanceTable(schema=...)`: nested models
  become struct columns, lists become list columns, `Field(description=...)`
  becomes column documentation, and unmappable fields fail loudly with a
  per-field `ava.Json` JSON-string opt-out.
- Model-declared tables accept model instances (single or list) in `append(...)`
  and read back as validated models via `read_models()`.
- `ava.AppendResult` is generic over the row model and adds `to_models()`,
  `one()` (asserts exactly-one-row cardinality), and `one_or_none()`.
- `ava.ModelStream.one()`, `one_or_none()`, and `all()` inject validated row
  models at workflow stream boundaries with explicit cardinality contracts and
  contextual errors, while preserving passthrough, table-backed, Ray, and rerun
  behavior.
- New `ava.input.<field>` build-time placeholder for feeding validated run
  input into any node's arguments.
- Fix: futures passed explicitly as keyword arguments are no longer re-bound
  implicitly by position.
- See [docs/data-model-api.md](docs/data-model-api.md#define-a-schema).
- Added bodyful `@ava.agent_step` / `@ava.agent.step` aliases in the base
  package. Steps receive a callable `ava.Agent`, handle raw DSPy predictions in
  their own Python body, and explicitly persist their results.
- `ava.Signature` is a subclassable native DSPy contract using
  `ava.InputField()` / `ava.OutputField()`. The identical
  `ava.agent.Signature` also builds inline string signatures; skills and tools
  are configured only by `@ava.agent_step(...)`.
- `@ava.workflow(agent_defaults={...})` supplies workflow-scoped PredictRLM
  runtime defaults; agent-step kwargs override them. Process-global agent
  configuration and automatic agent-step table/output behavior were removed.
- `ava.agent.skills`, `ava.agent.Skill`, and `ava.agent.File` lazily re-export
  the corresponding PredictRLM APIs.
- The agent inspector now has Trace and Metadata tabs. Metadata is available
  before execution and shows the resolved signature, skills, static
  instructions, tools, and effective redacted runtime configuration. Expanded
  metadata fields follow their rendered order and include selectable scalar
  leaves.
- Fix: live agent evidence now remains attached across async node execution, so
  the Trace tab receives turns and status updates under local and Ray backends.
- Agent inspector object controls are always visible and recursively expand or
  collapse only the selected subtree without changing expansion state when the
  selection moves. Large collections remain lazily paginated. Trace durations
  use seconds, completed traces infer terminal status when older envelopes omit
  it, and custom model types retain useful declaration metadata instead of
  dropping the pane.
- See [docs/agent-steps.md](docs/agent-steps.md).

## 0.1.0-rc0

Initial team release candidate for Avalanche as a local-first Python data-flow
toolkit.

### What works

- Python flow authoring with `@ava.source`, `@ava.step`, `@ava.dest`, and
  `@ava.workflow`.
- Local execution with `ava.LocalExecutor`.
- Iceberg-backed and Lance-backed storage helpers.
- Canonical smoke-tested examples under `examples/`.
- Stream and Cursor examples using the current provider APIs.
- Local operator startup against explicit flow files or directories.
- Connected TUI mode through the operator gRPC API.
- Mock TUI mode for UI-only exploration.
- Bounded smoke gate with `make smoke-test`.
- Full pre-commit gate with `make precommit-check`.

### Known limitations

- APIs, operational behavior, and packaging details may change before a stable
  release.
- Operator and TUI commands are local-development paths, not deployment guidance.
- Production auth, authorization, TLS, and multitenancy are out of scope.
- One-click cloud deploy and schema migration CLI are not implemented.
- Durable operator replay/recovery is limited to the current implementation.
- `--flows .` from the repository root is unsafe because discovery imports Python
  files; use a specific flow file or clean flow-only directory.
- Some tests may be skipped when optional local services or terminal features are
  unavailable.

### Team handoff

The official artifact for this release candidate is the Git repository. Start
with `README.md`.
