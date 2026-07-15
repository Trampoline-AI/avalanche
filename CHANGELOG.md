# Changelog

## Unreleased

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
