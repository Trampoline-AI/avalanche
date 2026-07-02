# Release File Manifest

This manifest defines the clean tree for the new release repository. The current
workspace remains the historical work area; the release repository is built by
copying only the paths listed here.

## Included

- `README.md`
- `ARCHITECTURE.md`
- `.gitignore`
- `Makefile`
- `pyproject.toml`
- `uv.lock`
- `docs/release-readiness.md`
- `docs/release-file-manifest.md`
- `scripts/export_release_tree.py`
- `examples/README.md`
- `examples/complex_dag_pattern.py`
- `examples/cursor_pattern.py`
- `examples/operator_workflow.py`
- `examples/stream_pattern.py`
- `src/avalanche/`
- `src/ava_cli/`
- `src/runtime/`
- `src/tui/`
- `test/`

Generated during export:

- `.release-tree-files.txt`

## Excluded

These stay only in the historical workspace unless a future release plan promotes
a specific file into the included list.

- `.avalanche/`
- `.claude/`
- `.hermes/`
- `.pytest_cache/`
- `.ruff_cache/`
- `.venv/`
- `art/`
- `dist/`
- `doc/`
- `htmlcov/`
- `journal/`
- `poc/`
- `scratch/`
- `spec/`
- `tasks/`
- Python bytecode caches and local coverage outputs

## Unresolved

- `LICENSE`: required before external publication.
- `CONTRIBUTING.md`: recommended before inviting external contributions.
- `CHANGELOG.md`: required before tagging an external alpha or beta.
- `CLAUDE.md`: keep only if the new repository intentionally carries agent
  workflow guidance for contributors.
