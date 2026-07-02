# Avalanche

Avalanche is an internal-alpha Python flow toolkit for local data experiments. It
combines a small DAG API with Iceberg-backed storage helpers, a local/Ray
execution layer, an operator process, and a terminal UI.

## Status

This repository is being prepared for internal alpha use. The current release
contract is documented in [docs/release-readiness.md](docs/release-readiness.md).
The clean tree for a new release repository is defined in
[docs/release-file-manifest.md](docs/release-file-manifest.md).

Use the `ava` command for internal-alpha operator and TUI flows. The package does
not expose an `avalanche` command or an `avalanche init` project generator.

## Setup

```bash
uv sync
```

```bash
uv run pytest
```

## Canonical Examples

The smoke-tested examples are listed in [examples/README.md](examples/README.md).

```bash
uv run python examples/complex_dag_pattern.py
uv run python examples/stream_pattern.py
uv run python examples/cursor_pattern.py
uv run python examples/operator_workflow.py
```

By default, examples write local artifacts under `.avalanche/examples/`. Set
`AVALANCHE_EXAMPLE_ROOT=/path/to/tempdir` to redirect those artifacts.

## Operator And TUI

Start a local operator and connected TUI in one command:

```bash
uv run ava dev --flows examples
```

Or start the operator and TUI separately. The TUI does not import example files;
it reads the flow list exposed by the operator over gRPC.

```bash
uv run ava operator --flows examples --port 7433
```

In another terminal:

```bash
uv run ava tui --connect localhost:7433
```

The Python module commands remain available as a fallback:

```bash
uv run python -m avalanche.operator --flows examples --port 7433
```

```bash
uv run python -m avalanche.tui --connect localhost:7433
```

The TUI can also run in mock mode without an operator:

```bash
uv run python -m avalanche.tui
```

## Current Non-Goals

- Production authentication, authorization, TLS, or multitenancy.
- One-click cloud deploy.
- Schema migration CLI.
- Production durability guarantees for operator replay and recovery.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the implemented component model and
runtime boundaries.
