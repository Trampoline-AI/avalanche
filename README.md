<p align="center">
  <img align="center" src="docs/assets/brand/avalanche-logo-3d.png" width="560px" alt="Avalanche logo" />
</p>

# Avalanche

Avalanche is a Python toolkit for local data-flow experiments. It combines a
small DAG API, Iceberg and Lance storage helpers, local or Ray-backed execution,
an operator process, and a terminal UI for monitoring and control.

## Status

Avalanche is ready for early users to try locally, but APIs, operational
behavior, and packaging details may change before a stable release. The current
team release candidate is `0.1.0-rc0`.

Use the `ava` command for operator and TUI flows. The package does not expose an
`avalanche` command or an `avalanche init` project generator.

For the full getting-started guide, see [docs/getting-started.md](docs/getting-started.md).
For API guides, see [docs/dag-api.md](docs/dag-api.md),
[docs/data-model-api.md](docs/data-model-api.md), and
[docs/agent-steps.md](docs/agent-steps.md). Platform integrations that materialize
worker inputs or publish task-scoped outputs should start with
[docs/execution-services.md](docs/execution-services.md).
For implementation boundaries, see [ARCHITECTURE.md](ARCHITECTURE.md).
For release notes, see [CHANGELOG.md](CHANGELOG.md).

## Setup

```bash
uv sync
```

Run the full test suite:

```bash
make test
```

Run the full pre-commit gate before handing off changes:

```bash
make precommit-check
```

Run the shorter end-to-end smoke gate:

```bash
make smoke-test
```

## Local Development Modes

Avalanche supports two ways to run flows locally. Both execute the same
`Workflow.run()` path with a `LocalExecutor` or `RayExecutor`; the difference is
which process owns the run lifecycle.

### Embedded Mode

Run the Python program that declares the flow and call `Workflow.run()` directly.
It immediately returns an awaitable `ava.RunHandle` with the run ID. Block for
the output with `.result()` or use `await run` from asynchronous code:

```python
run = workflow.run(executor=ava.LocalExecutor())
print(run.run_id)
result = run.result()
```

The handle is process-local and its non-daemon driver thread keeps the embedded
process alive until the run finishes. `run.cancel()` requests cooperative
cancellation between node submissions; it does not forcibly stop an active
thread or Ray task. Durable run state remains an operator responsibility.

Start with the simplest smoke-tested example:

```bash
uv run python examples/complex_dag_pattern.py
```

More examples are documented in [examples/README.md](examples/README.md):

```bash
uv run python examples/stream_pattern.py
uv run python examples/cursor_pattern.py
uv run python examples/operator_workflow.py
```

Examples that need storage write under `.avalanche/examples/` by default. Set
`AVALANCHE_EXAMPLE_ROOT=/path/to/tempdir` to redirect those artifacts.

### Operator-Managed Mode

Start the operator separately and point it at the Python files or directories
that declare your flows. The operator discovers those flows, executes runs, and
owns run state, logs, cancellation, schedules, and file watching. The CLI and TUI
are clients of the operator; the TUI does not execute flows itself.

Start a local operator and connected TUI in one interactive command:

```bash
uv run ava dev --flows examples
```

Or run the operator and TUI in separate terminals. The TUI does not import
example files directly; it reads the flow list exposed by the operator over gRPC.

```bash
uv run ava operator --flows examples --port 7433
```

In another terminal, start a run from the CLI:

```bash
uv run ava run operator_demo_workflow --connect localhost:7433
```

Or connect the TUI and start runs interactively:

```bash
uv run ava tui --connect localhost:7433
```

The Python module entry points are available as a fallback:

```bash
uv run python -m avalanche.operator --flows examples --port 7433
uv run python -m avalanche.tui --connect localhost:7433
```

The TUI can also run in mock mode without an operator:

```bash
uv run python -m avalanche.tui
```

## Optional Components

The development environment installed by `uv sync` includes the tooling used by
the tests. Package consumers can install narrower extras:

| Extra | Purpose |
| --- | --- |
| `runtime` | operator gRPC, file watching, and scheduling dependencies |
| `tui` | Textual terminal UI dependencies |
| `ray` | Ray executor dependencies |
| `lance` | Lance storage backend dependencies |
| `agent` | agent-backed steps through [PredictRLM](https://github.com/Trampoline-AI/predict-rlm) |
| `all` | all optional runtime components |

For local development, sync extras with commands such as:

```bash
uv sync --extra tui --extra runtime
```

## Current Caveats

- Operator and TUI commands are local-development paths, not deployment guidance.
- Production auth, authorization, TLS, and multitenancy are out of scope.
- One-click cloud deploy and schema migration CLI are not implemented.
- Durable operator replay/recovery is limited to the current implementation.
- `--flows .` from the repository root is unsafe because discovery imports Python
  files; use a specific flow file or clean flow directory.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development commands and review
expectations.

## License

Avalanche is licensed under the [Apache License 2.0](LICENSE).
