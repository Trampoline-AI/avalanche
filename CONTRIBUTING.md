# Contributing

Keep changes small, tested, and honest about what is implemented.

## Local Setup

```bash
uv sync
```

## Quality Gates

Run the full test suite:

```bash
make test
```

Run the full pre-commit gate before handing off changes:

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
- Public CLI docs should say `flow` and `--flows`.
- Keep historical or speculative material out of onboarding docs unless it is
  clearly marked as non-release context.

## Optional Components

The default development sync installs the dependencies needed for the current
test suite. Optional package extras are `runtime`, `tui`, `ray`, `lance`, and
`all`; document new optional dependencies in `pyproject.toml`, `README.md`, and
`docs/getting-started.md` together.
