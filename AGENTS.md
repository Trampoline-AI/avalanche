# Repository Guidelines

## Project Overview

Avalanche is a local-first Python toolkit for defining and running data-flow DAGs. It combines a small decorator-based workflow API, Iceberg and Lance storage helpers, Local and Ray execution, an optional operator/gRPC control plane, and a Textual TUI. The project is an early release candidate: keep operational claims local-development focused and do not imply production auth, multi-tenancy, deployment, or durable recovery that does not exist.

## Architecture & Data Flow

1. **DAG authoring:** `src/avalanche/dag.py` captures decorated node calls inside a `ContextVar`-scoped `@workflow`. `NodeFuture`, `>>`, and `&` describe dependencies; workflow bodies should define graph edges rather than perform runtime work.
2. **Embedded execution:** `Workflow.run()` creates an awaitable `RunHandle` and starts a driver thread. The driver schedules nodes topologically, resolves annotated runtime providers and dependency references, submits work to `LocalExecutor` or `RayExecutor`, and fetches only final outputs. Payloads, status, lineage, and execution receipts remain separate channels.
3. **Runtime injection and storage:** providers in `src/avalanche/runtime/providers/` inject streams, models, logging, run input, and context. Backend-neutral contracts live in `src/avalanche/storage.py`; Iceberg and Lance implementations preserve snapshot and rerun lineage.
4. **Operator mode:** `src/runtime/operator/` discovers workflow descriptors, runs each workflow in a spawned coordinator process, and exchanges serializable lifecycle events over queues. The gRPC server exposes discovery, run control, logs, and update streams.
5. **TUI:** `GrpcStateProvider` implements the `StateProvider` boundary. `src/tui/ui_store.py` is the single mutable UI state owner; background threads enqueue guarded updates that the Textual UI applies on its own thread.

Preserve these boundaries. Do not pass live `Workflow` objects across operator or TUI process boundaries, collapse distributed references into eager local values, or bypass provider-based dependency injection.

## Key Directories

- `src/avalanche/`: public library API, DAG construction/execution, runtime models, storage contracts, Iceberg/Lance backends, and agent support.
- `src/runtime/`: executor implementations and the operator runtime, including discovery, scheduling, workers, gRPC, and generated protobuf modules.
- `src/tui/`: Textual application, widgets, DAG layout, mock provider, and `UIStore`.
- `src/ava_cli/`: the `ava` command-line entry point.
- `test/`: behavior and integration tests, with focused suites under `operator_tests/`, `storage/`, `iceberg/`, and `agent/`.
- `examples/`: smoke-tested executable workflows covering DAGs, streams, cursors, and operator discovery.
- `docs/`: architecture, API, execution-service, agent, and release guidance.
- `scripts/`: maintenance and performance tooling, including `benchmark_tui.py`.

## Development Commands

Use repository commands rather than ad hoc tool invocations:

```bash
uv sync --all-extras                 # install the complete development environment
uv run pre-commit install            # enable repository commit hooks once per clone
make lint                            # Ruff checks
make format                          # Ruff fixes and formatting
make test                            # full pytest suite with all extras
make precommit-check                 # lint + full tests; `make check` is an alias
make smoke-test                      # bounded documented-user-path tests
make test-cov                        # terminal branch coverage
make test-cov-html                   # HTML coverage report
make tui-bench                       # enforce TUI refresh budgets
make web-lint                       # ESLint checks for the browser UI
make web-format                     # format the browser UI with Prettier
make web-format-check               # check browser formatting without changes
make web-assets-check                # rebuild and reject stale packaged web assets
uv run pytest test/foo_test.py -q    # focused test while iterating
uv build                             # required after packaging/entry-point changes
```

Useful runtime examples:

```bash
uv run python examples/complex_dag_pattern.py
uv run ava dev --flows examples
uv run ava operator --flows examples --port 7433
uv run ava tui --connect localhost:7433
uv run ava tui                            # mock TUI
```

Avoid `--flows .`: operator discovery imports Python files recursively. Use a narrow file or directory.

## Code Conventions & Common Patterns

- Target Python 3.11–3.13. Use modern precise annotations, `Protocol`,
  dataclasses, and Pydantic models. Treat the declared domain schema as the only
  valid in-process shape.
- Ruff enforces `E`, `F`, `W`, `I`, and `N`; line length is 96. Keep imports first-party-aware for `avalanche`, `ava_cli`, `runtime`, and `tui`.
- Use `snake_case` for functions/modules/node symbols and `PascalCase` for classes. Prefer descriptive domain names such as `RunHandle`, `WorkflowInfo`, and `ExecutionServicesSpec`.
- Do not introduce `Any` into handwritten code. At genuinely untyped
  third-party or serialization boundaries, isolate it in an adapter and
  validate immediately into the concrete domain schema. Do not use vague
  `object` payloads, untyped dictionaries, broad unions, or `cast(...)` to
  accommodate hypothetical shapes.
- Validate or deserialize once at an actual external boundary, then use typed
  attributes directly. Do not probe known models with `getattr`, `hasattr`,
  dictionary fallbacks, or alternate-shape branches.
- Do not silently coerce unexpected values with `str(...)`, `int(...)`,
  `list(...)`, truthiness normalization, or empty/default substitutes. A
  conversion belongs only at a declared boundary whose contract requires it.
- Optional fields and defaults must represent real domain states, not defensive
  accommodation. Unexpected absence, type, shape, or enum values should raise
  immediately through Pydantic validation or an explicit domain error.
- Prefer simple code that fails loudly over defensive code that guesses,
  recovers, skips, or continues with partial data. Catch an exception only when
  the contract defines a specific recovery or when translating it at a system
  boundary; never catch broadly to preserve a best-effort path.
- Raise explicit domain or validation errors with actionable messages. Operator boundaries translate them to appropriate gRPC statuses. Preserve a primary failure when cleanup also fails; attach cleanup context rather than replacing it.
- Respect sync/async interoperability. Use the existing `call_sync_or_async` path instead of blindly nesting `asyncio.run()`. Preserve `ContextVar` state when work crosses threads.
- Prefer immutable descriptors (`frozen=True`, mapping proxies) and locks around shared mutable runtime state. TUI worker threads must enqueue updates rather than mutate widgets.
- Runtime dependency injection is annotation/provider based. Extend the binding plan or `ParameterProvider` patterns instead of adding special-case argument plumbing.
- Preserve Ray object references and transport envelopes until a consumer requires materialization; avoid unnecessary copies or eager fetches.
- Generated protobuf files under `src/runtime/operator/proto/` have dedicated Ruff ignores; regenerate them through the Make target rather than hand-editing.

## Important Files

- `src/avalanche/__init__.py`: supported public exports.
- `src/avalanche/dag.py`: graph model, decorators, binding, scheduling, and `Workflow.run()`.
- `src/avalanche/run_handle.py`: thread-backed awaitable run lifecycle and cancellation.
- `src/avalanche/storage.py`: backend-neutral namespace/table contracts.
- `src/runtime/executor.py`: Local/Ray executor protocol and result normalization.
- `src/runtime/operator/operator.py`: operator state and run-process orchestration.
- `src/runtime/operator/run_worker.py`: spawn-safe workflow execution worker.
- `src/runtime/operator/server.py` and `client.py`: gRPC boundary and TUI provider.
- `src/tui/app.py` and `ui_store.py`: TUI event loop and state management.
- `pyproject.toml`: dependencies, extras, packaging, pytest, Ruff, and coverage configuration.
- `Makefile`: canonical development and maintenance commands.
- `ARCHITECTURE.md`: operator, executor, TUI, and execution-services design.
- `.github/workflows/ci.yml`: standard, Ray, and tmux CI gates.

## Runtime/Tooling Preferences

- Use **uv** for dependency management and command execution; keep `uv.lock` synchronized. Do not introduce pip/Poetry workflows.
- Packaging uses Hatchling with a `src/` layout containing four import roots: `avalanche`, `ava_cli`, `runtime`, and `tui`.
- The supported CLI is `ava`; do not document an `avalanche` command or unimplemented initialization flow.
- Optional extras are `ray` and `lance`. When adding an extra, synchronize
  `pyproject.toml`, README, and getting-started documentation.
- Ray tests require a local Ray runtime. Real-terminal TUI tests require the `tmux` executable. Operator integration uses local processes and gRPC ports.
- Use `AVALANCHE_EXAMPLE_ROOT` to isolate example storage artifacts when needed.

## Testing & QA

- Pytest is the test framework. Async tests must use `@pytest.mark.asyncio`; strict asyncio mode is enabled.
- Mark Ray tests with `@pytest.mark.ray` and gate optional packages with `pytest.importorskip`. Mark real-terminal tests with `@pytest.mark.tmux`.
- Add behavior-level regression tests near the affected subsystem. Prefer `tmp_path`, explicit fakes/recording services, `pytest.raises`, and meaningful parametrized matrices over source-text or plumbing assertions.
- Do not add standalone logging tests or assert exact log-message text. When
  logging matters to an existing behavior contract, verify that logging occurs
  within that behavior test without coupling the assertion to message wording.
- Do not assert exact user-facing copy unless the wording itself is the behavior under
  test. Prefer roles, visibility, state transitions, and accessible semantics; never
  duplicate labels in tests merely to locate UI.
- Iterate with the smallest focused tests that cover the current feature or fix;
  never use the full regression suite as the iteration loop.
- `make test` is an exceptional validation gate, not a default final step. Do
  not run it for ordinary bug fixes or new features; CI covers broad
  regressions. Run it only for an explicitly requested or approved large-scale
  refactor or migration.
- TUI behavior normally needs both headless Textual coverage (`test/tui_test.py`) and tmux coverage (`test/tui_tmux_test.py`) when terminal rendering, sizing, or input behavior changes.
- CI installs all extras, runs Ruff, then separates ordinary tests (`not tmux and not ray`), Ray tests, and tmux tests into distinct jobs.
- Branch coverage is configured for `src/avalanche`; `make test-cov` reports missing lines. Do not treat a focused green test as proof for unrelated runtime, Ray, or tmux paths.
- Run `make smoke-test` for changes affecting documented examples or the main user path, and `uv build` for metadata or entry-point changes.

## Commit Messages

- Every commit message MUST start with a lowercase category prefix followed by a
  colon (`<prefix>:`). When a change concerns a specific repository area, include
  that area as a lowercase scope (`<prefix>(<area>):`), for example
  `feat(operator):` or `fix(tui):`. Use unscoped `dev:` for development tooling,
  agent skills, and repository-wide contributor guidance.

## Pull Request Descriptions

Every PR MUST add an entry under `## Unreleased` in `CHANGELOG.md` describing
its user-facing change.

Every PR description must be self-contained, use plain and direct language, and start with
these sections in this order:

Avoid jargon. Explain an internal term the first time it matters.

```markdown
## Rationale
Explain in plain words why the change is needed and what it means for users.

## Summary
Explain in plain words what changed. Assume the reader has no prior conversation or commit context.

## Test Plan
- [ ] List the exact commands or manual scenarios used for verification.
- [ ] Include specialized Ray, tmux, packaging, or smoke checks when relevant.
```

Do not begin with implementation details or refer to earlier conversations, prompts, or commits as context.
