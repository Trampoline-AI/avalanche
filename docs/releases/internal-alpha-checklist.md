# Internal Alpha Checklist

Release candidate: `0.1.0-rc2`

Use this checklist from a fresh clone when validating the team handoff. The
official artifact is the Git repository.

Before or after the smoke path, skim the API guides:

- [DAG API](../dag-api.md)
- [Data Model API](../data-model-api.md)

## Prerequisites

- Python 3.11 through 3.13.
- `uv` installed locally.
- A terminal environment that can run local Python commands.

## Setup

```bash
uv sync
```

## Required gates

Run the full test suite:

```bash
make test
```

Run the full pre-commit gate:

```bash
make precommit-check
```

Run the shorter end-to-end smoke gate:

```bash
make smoke-test
```

Expected result for this release candidate: all required gates pass. Some tests
may be skipped when optional local services or terminal features are unavailable.

## Local flow path

Run the simplest canonical example:

```bash
uv run python examples/complex_dag_pattern.py
```

Optional additional examples:

```bash
uv run python examples/stream_pattern.py
uv run python examples/cursor_pattern.py
uv run python examples/operator_workflow.py
```

Examples that need storage write under `.avalanche/examples/` by default. Set
`AVALANCHE_EXAMPLE_ROOT=/path/to/tempdir` to isolate artifacts.

## Operator and TUI path

Start a local operator and connected TUI together:

```bash
uv run ava dev --flows examples
```

Or start them separately:

```bash
uv run ava operator --flows examples --port 7433
```

In another terminal:

```bash
uv run ava tui --connect localhost:7433
```

The TUI reads flows from the operator over gRPC. It does not import examples
directly.

## Package/build smoke

When package metadata changes, verify the build:

```bash
uv build
```

## Known caveats

- This is a release candidate for team validation, not a stable release.
- Operator and TUI commands are local-development paths, not deployment guidance.
- Production auth, authorization, TLS, and multitenancy are out of scope.
- One-click cloud deploy and schema migration CLI are not implemented.
- Durable operator replay/recovery is limited to the current implementation.
- Avoid `--flows .` from the repository root; use a specific flow file or clean
  flow-only directory.

## Feedback routing

Record blockers and high-signal friction as GitHub issues or in the team's
chosen tracking channel. Convert feedback into explicit follow-up tasks before
broadening the audience.
