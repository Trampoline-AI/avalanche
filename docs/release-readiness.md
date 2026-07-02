# Release Readiness

This document is the canonical contract for the current internal alpha gate. It
describes what teammates can rely on while the repository is being prepared for a
broader external alpha.

## Internal Alpha Persona

The internal alpha user is a teammate engineer who can clone this repository and
run local data flow experiments. They are comfortable with Python, `uv`, and
local terminals. They are not an external production user and should not expect a
packaged, hosted, or security-hardened service.

## Supported Internal Alpha Paths

- Python API authoring with `@ava.source`, `@ava.step`, `@ava.dest`, and
  `@ava.workflow`.
- Local flow execution with `ava.LocalExecutor`.
- Local Iceberg examples backed by PyIceberg's SQL SQLite catalog.
- Stream-based incremental processing with explicit `ava.Stream(..., key=...)`
  consumer keys.
- Cursor-based manual checkpoint examples for advanced incremental flows.
- Operator startup against a flow file or directory with the local executor.
- Operator startup with the Ray executor when a local Ray environment is already
  available.
- TUI mock mode and TUI connected mode against a local operator.
- `ava operator`, `ava tui`, and `ava dev` for internal-alpha local flows.

## Non-Goals For Internal Alpha

- One-click cloud deploy.
- Schema migration CLI.
- Production authentication, authorization, TLS, or multitenancy.
- Durable operator replay/recovery beyond the implementation that exists today.
- A public `avalanche` console command.
- An `avalanche init` project generator.

## Current Baseline

The release-readiness audit that produced this plan recorded the following
baseline:

- `uv run pytest` passed with `334 passed, 45 skipped, 16 warnings`.
- `uv build` succeeded and created wheel/sdist artifacts.
- `make check` failed because `ruff check src/ test/` reported 63 errors.
- The old README advertised `uvx avalanche init` and `pipx run avalanche init`,
  but no matching console script existed.
- Several examples used stale `ava.Stream(table)` / `.read()` patterns instead
  of the current injected-`DataFrame` provider API.
- Public documentation still contained placeholders, missing links, and older
  branding artifacts.

## Internal Alpha Acceptance Checklist

- A teammate can run `uv sync` and `uv run pytest` from a clone.
- The examples README lists only canonical examples that are smoke-tested.
- At least one local flow example runs end-to-end without code edits.
- Stream and Cursor examples use DataFramely schema classes and current runtime
  provider APIs.
- Example artifacts write under `.avalanche/examples/` by default, or under a
  caller-provided temp directory via `AVALANCHE_EXAMPLE_ROOT`.
- The operator can discover a documented flow file or directory.
- The TUI can run in mock mode or connect to a local operator.
- README commands do not promise unimplemented CLI, migration, cloud deploy, or
  production auth behavior.
- `make check` is the source-tree quality gate: Ruff over source, tests, and
  scripts, followed by the full pytest suite.
- The new release repository is built from the allowlist in
  [release-file-manifest.md](release-file-manifest.md), not from this full
  historical workspace.

## Caveats

- The default operator and TUI commands are local development paths, not
  production deployment guidance.
- `ava` is an internal-alpha command surface, not a production deployment tool.
- Older documents under `doc/`, `spec/`, `journal/`, `scratch/`, and `poc/` stay
  behind unless a future release plan promotes a specific file into the manifest.
