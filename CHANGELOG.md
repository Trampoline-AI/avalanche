# Changelog

## Unreleased

### Operator transport

- Operator streams now replay bounded, typed run deltas under an instance epoch
  and explicitly require a structural reset for stale cursors or restarts.
- Remote TUI state applies deltas in sequence, ignores duplicates, and reloads
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
- See [docs/data-model-api.md](docs/data-model-api.md#pydantic-model-schemas).
- New optional `agent` extra (`uv sync --extra agent`) with bodyful
  `@ava.agent_step` / `@ava.agent.step` aliases. Steps receive a callable
  `ava.Agent`, handle raw DSPy predictions in their own Python body, and
  explicitly persist their results.
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
  instructions, tools, and effective redacted runtime configuration.
- Fix: live agent evidence now remains attached across async node execution, so
  the Trace tab receives turns and status updates under local and Ray backends.
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
with `README.md`, then follow `docs/getting-started.md` and
`docs/releases/internal-alpha-checklist.md`.
