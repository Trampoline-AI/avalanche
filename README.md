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
Terminal values may contain `ava.File` or `ava.Workspace` directly or nested in
supported Pydantic/list/tuple/dict results. Embedded `.result()` returns the
original Python shape and portable file/workspace objects. A terminal
`Workspace` carries its serializable manifest; its local `.path` exists only
while Avalanche is executing user node code.

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

The operator listens on `127.0.0.1` by default because its gRPC service does not
provide built-in authentication. Binding another interface with `--host` is an
explicit deployment choice and requires an external trusted and authenticated
boundary. Loopback limits network reachability; it does not identify callers,
and any process running as a local user may attempt to call the service.

In another terminal, start a run with JSON fields and a top-level file input.
`--file FIELD=PATH` reads and attaches the file bytes; it does not send the local
path to the operator:

```bash
RUN_ID=$(
  uv run ava run document_file_workflow \
    --connect localhost:7433 \
    --input '{"value": 41}' \
    --file document=./doc.txt
)
```

This command targets the bundled
[`document_file_workflow`](examples/document_file_workflow.py) example.

Download the successful terminal result without printing binary bytes. `--wait`
waits up to `--timeout` seconds for a nonterminal run:

```bash
uv run ava result "$RUN_ID" \
  --connect localhost:7433 \
  --wait \
  --output-dir ./run-result
```

The output directory must not already exist, and its parent directory must
exist. The CLI writes and verifies the complete result in a private staging
directory inside a retained, identity-pinned holding directory, syncs it, and
atomically renames the staged name without replacing an existing destination.
It immediately opens the requested destination through the retained parent
descriptor and compares its identity to the retained staging descriptor.
Substitution fails closed and triggers bounded, descriptor-anchored cleanup.
The published directory contains collision-resistant attachment filenames and a
generated `result-<uuid>.json` with the run ID, reconstructed result shape,
original file metadata, relative paths, sizes, and SHA-256 digests. See
[Run input and context](docs/dag-api.md#run-input-and-context) and
[Workflow results](docs/dag-api.md#workflow-results) for the Python and CLI
contracts, limits, and complete output layout.

The output parent is a caller-owned local namespace. POSIX and macOS provide no
portable operation that renames an open directory descriptor, or conditionally
renames a source name only if it still identifies a specific inode. A hostile
concurrent process running as the same user can therefore create a transient
wrong destination before the CLI detects and removes it; that concurrency is
outside this local CLI threat model. Descriptor-authenticated catchable state is
cleaned, and catchable failures leave no requested destination. An interruption
after the holding `mkdir` side effect but before descriptor acquisition can
leave private empty holding residue: safe cleanup cannot distinguish the
created directory from a same-name replacement, so it does not open, adopt, or
remove that entry. The requested destination remains absent. An uncatchable
termination such as `SIGKILL` can also leave private holding residue.

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
