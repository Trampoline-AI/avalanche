# Getting Started

This guide is for early users trying Avalanche locally from a fresh clone. It
assumes Python 3.11 through 3.13, `uv`, and a local terminal.

## What Is Supported

- Python flow authoring with `@ava.source`, `@ava.step`, `@ava.dest`, and
  `@ava.workflow`.
- Local flow execution with `ava.LocalExecutor`.
- Iceberg-backed and Lance-backed storage helpers.
- Local Iceberg examples backed by PyIceberg's SQL SQLite catalog.
- Stream-based incremental processing with explicit `ava.Stream(..., key=...)`
  consumer keys.
- Cursor-based manual checkpoints for advanced incremental flows.
- A local operator that discovers flow files or directories.
- A Textual TUI in mock mode or connected to a local operator.
- Optional Ray-backed execution when a local Ray environment is available.

## What Is Not Supported Yet

- Production auth, authorization, TLS, or multitenancy.
- One-click cloud deploy.
- A schema migration CLI.
- A public `avalanche` console command.
- An `avalanche init` project generator.
- Durable operator replay/recovery beyond the current implementation.

## Setup

From the repository root:

```bash
uv sync
```

Run the default release gate:

```bash
make check
```

The gate runs Ruff over source and tests, then runs the full pytest suite. Some
tests are skipped when optional local services or terminal features are not
available.

## Run A Local Flow

Start with the simplest local DAG example:

```bash
uv run python examples/complex_dag_pattern.py
```

Then try the incremental examples:

```bash
uv run python examples/stream_pattern.py
uv run python examples/cursor_pattern.py
```

Examples that need storage write under `.avalanche/examples/` by default. To keep
artifacts isolated for a test run, set `AVALANCHE_EXAMPLE_ROOT`:

```bash
AVALANCHE_EXAMPLE_ROOT=/tmp/avalanche-example uv run python examples/stream_pattern.py
```

## Run The Operator And TUI

The shortest interactive path starts the operator and connected TUI together:

```bash
uv run ava dev --flows examples
```

To run them separately, start the operator in one terminal:

```bash
uv run ava operator --flows examples --port 7433
```

Then connect the TUI from another terminal:

```bash
uv run ava tui --connect localhost:7433
```

The TUI gets flows from the operator over gRPC. It does not import files from the
examples directory directly.

Module entry points are available if you need to bypass the `ava` wrapper:

```bash
uv run python -m avalanche.operator --flows examples --port 7433
uv run python -m avalanche.tui --connect localhost:7433
```

For UI-only exploration, use mock mode:

```bash
uv run python -m avalanche.tui
```

## Optional Components

`uv sync` installs the development dependency group used by the test suite.
Package consumers can choose narrower extras:

| Extra | Use |
| --- | --- |
| `runtime` | operator gRPC server/client, file watching, and scheduling |
| `tui` | Textual terminal UI |
| `ray` | Ray executor support |
| `lance` | Lance storage backend support |
| `all` | all optional runtime components |

For development, include extras with `uv sync --extra <name>`, for example:

```bash
uv sync --extra runtime --extra tui
```

## Troubleshooting

- If operator discovery imports too many files, replace `--flows .` with a
  specific flow file or a clean flow-only directory.
- If the connected TUI shows no flows, confirm the operator was started with the
  same port used by `ava tui --connect`.
- If Ray execution fails, first verify a local Ray head is available and set
  `RAY_ADDRESS` when needed.
- If example artifacts affect a repeat run, remove `.avalanche/examples/` or set
  `AVALANCHE_EXAMPLE_ROOT` to a fresh temp directory.

## Next References

- [examples/README.md](../examples/README.md) for canonical example details.
- [ARCHITECTURE.md](../ARCHITECTURE.md) for component boundaries.
- [CONTRIBUTING.md](../CONTRIBUTING.md) for local development expectations.
