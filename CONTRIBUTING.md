# Contributing

Keep changes small, tested, and honest about what is implemented.

## Local Setup

```bash
uv sync --all-extras
pnpm --dir web install --frozen-lockfile
```

## Quality Gates

Run the core regression collection with one command (requires `tmux`):

```bash
make test
```

This runs local Python scenarios, then isolated Ray and real-terminal scenarios,
then browser tests. Ordinary Python scenarios run concurrently; Ray and tmux
remain serial because they own process-global resources. CI runs these same
groups in separate jobs.

Keep this collection focused on:

- DAG execution, runtime injection, failure, cancellation, and rerun lineage.
- Storage read/write integrity, concurrent commits, cursors, and safe file publication.
- Operator discovery, spawned execution, transport recovery, and trust boundaries.
- UI selection, run controls, live-state ordering, inspection, and recovery.

UI tests protect interactions and displayed data, not every widget, label, color,
or layout coordinate. Generated schema inventories, static documentation checks,
mock forwarding, and repeated happy paths do not belong in the suite. Add a test
only when a plausible regression would change an observable result; consolidate
overlapping scenarios instead of accumulating a test for every branch.

This is core regression coverage, not a promise of exhaustive API, visual, or
platform compatibility. Run `make tui-bench` and `make web-bench` when changing
refresh or large-run behavior.

For the aggregate Python lint, Python regression, and browser test gate:

```bash
make precommit-check
```

Run the bounded smoke-test gate when checking the documented user path:

```bash
make smoke-test
```

Useful focused commands:

```bash
uv run ruff check src/ test/
uv run pytest test/example_smoke_test.py -v
uv run pytest test/cli_test.py -v
make web-test
make web-lint
```

Build artifacts when package metadata or entry points change:

```bash
uv build
```

## Bug Fixes

- Reproduce bugs with a focused failing test before changing production code.
- Keep regression tests focused on user-visible behavior or second-order effects.
- Run the focused test first, then the relevant broader gate.

## Documentation

- Document only commands that exist and are tested or clearly marked interactive.
- Use `ava` for the CLI command. Do not document an `avalanche` console command.
- Public CLI docs should explain that `operator` and `dev` use explicit
  positional flow targets or `[tool.avalanche].flow_targets`, not a `--flows`
  flag or a current-directory default.
- Keep historical or speculative material out of onboarding docs unless it is
  clearly marked as non-release context.

## Optional Components

The default development sync installs the dependencies needed for the current
test suite. Optional package extras are `ray` and `lance`; document new optional
dependencies in `pyproject.toml` and `README.md` together.
