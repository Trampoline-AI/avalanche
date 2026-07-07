# Stream Reruns Implementation Review

## Verdict

**APPROVE WITH NITS.** The rerun-specific blockers from the original review are
fixed and verified. Sparse rerun-of-rerun overlay now resolves parent rows,
skipped non-stream upstreams fail at validation, and lineage vectors propagate
through ordinary Python-arg dependencies across Local and Ray. The scan-mode
backlog-drain issue is not fixed, but it is now explicitly deferred in
`tasks/run-rerun-stream-plan.md` and is not on the rerun read path.

Remaining nits are documentation/cleanup level: document the generated slug
suffix contract more explicitly if operators will address repeated default nodes,
and keep the deferred scan-mode follow-up visible.

## Scope and evidence

Reviewed current uncommitted worktree against `tasks/run-rerun-stream-plan.md`.

Checkpoint before this follow-up:

- `/tmp/avalanche-rerun-followups-before.patch`

Verification commands and observed results:

- Direct semantic probe: `uv run python /tmp/avalanche_rerun_review_probes.py`
  ```text
  sparse.base ['alpha', 'beta']
  sparse.lazy1 ['alpha', 'beta']
  sparse.lazy2_from_lazy1 ['alpha', 'beta']
  implicit.error ValueError Rerun node 'middle_1' has an implicit dependency on
    skipped upstream node 'load_1' bound to a non-stream parameter; v1 reruns
    require skipped upstream data to be consumed through ava.Stream
  lineage.vectors ['{"load-data":"source_run","process-data":"rerun_run","sink":"rerun_run"}']
  ```
- `uv run pytest -q --tb=short -r fEsxX test/rerun_test.py` ->
  `20 passed, 1 warning`.
- `uv run pytest -q --tb=short -r fEsxX test/rerun_test.py test/dag_test.py
  test/run_context_test.py test/storage/test_table_contracts.py` ->
  `72 passed, 15 skipped, 1 warning`.
- `uv run pytest -q --tb=short -r fEsxX test/streaming_test.py
  test/stream_serialization_test.py test/storage/test_stream_contracts.py` ->
  `22 passed, 12 skipped, 1 warning`.
- `uv run ruff check src/ test/` -> `All checks passed!`.
- `git diff --check` -> clean.
- `make precommit-check` -> `417 passed, 48 skipped, 21 warnings`.
- Independent read-only `LineagedResult` audit returned `APPROVE`. Its only
  suggested non-blocking gap was hook-replacement lineage reattachment, now
  covered by `test/rerun_test.py:554`.

## Findings

### 1. FIXED: sparse lazy rerun-of-rerun overlay resolves the parent view

Previously, rerun stream reads discovered `_ava_rerun_of` only from the same
slug-filtered payload scan. A lazy intermediate rerun with no rows for the input
producer stopped parent-chain resolution and returned an empty frame.

Current implementation:

- Rerun row resolution is centralized in `_read_rerun_rows()` and overlayed by
  `_overlay_rerun_frames()`:
  `src/avalanche/runtime/providers/stream.py:474` and
  `src/avalanche/runtime/providers/stream.py:507`.
- Regression coverage: `test/rerun_test.py:304`.
- Probe proof: `sparse.lazy2_from_lazy1 ['alpha', 'beta']`.

### 2. FIXED: skipped implicit non-stream upstreams fail at validation

Previously, validation only inspected explicit `args`/`kwargs`, so implicit chain
parents could be pruned and later fail as raw Python `TypeError`s during node
submission.

Current implementation:

- `_validate_no_skipped_non_stream_inputs()` validates scheduled nodes against
  skipped original parents: `src/avalanche/dag.py:792`.
- Rerun planning invokes it before returning the pruned execution order:
  `src/avalanche/dag.py:1572`.
- Regression coverage: `test/rerun_test.py:350`.
- Probe proof: skipped implicit parent now raises the intended `ValueError` that
  says skipped upstream data must be consumed through `ava.Stream`.

### 3. FIXED: lineage vectors propagate through Python-arg dependencies

Previously, producer lineage merged only through `Stream` inputs. Downstream nodes
that consumed scheduled upstream results as ordinary Python args wrote only their
own node into `_ava_lineage_vector`.

Current implementation:

- `LineagedResult` carries producer lineage across executor boundaries:
  `src/avalanche/types.py:68`.
- Node wrapper merges and strips envelopes before user code runs, then rewraps
  node results: `src/avalanche/dag.py:1121` and
  `src/avalanche/dag.py:1140`.
- Final workflow returns unwrap before crossing the public API:
  `src/avalanche/dag.py:1251`.
- Implicit args, explicit args, indexed tuple returns, multi-return values,
  `AppendResult` zero-copy, and Ray refs are covered by targeted tests:
  `test/rerun_test.py:371`, `test/rerun_test.py:420`,
  `test/rerun_test.py:463`, and `test/rerun_test.py:508`.
- Probe proof: sink row vector now includes
  `load-data`, `process-data`, and `sink` with the expected run IDs.

### 4. DEFERRED: scan-mode backlog drain prerequisite

Table-backed scan mode still claims one pending snapshot per consume:
`src/avalanche/runtime/providers/stream.py:324`. This was a plan prerequisite in
the original two-pager, but it has been explicitly split out of the rerun branch:
`tasks/run-rerun-stream-plan.md:114` and the updated DoD at
`tasks/run-rerun-stream-plan.md:137`.

This is acceptable for the rerun review because rerun mode uses row-lineage
filtered scans and bypasses `ProgressStore`. It remains a real follow-up for
normal scan mode.

### 5. NIT: generated slug suffixes should be documented as operator-visible

Repeated default node calls receive generated suffixes (`slug`, `slug_2`, ...).
That behavior is covered in tests, but because rerun starts are operator-facing,
docs should explicitly recommend `slug=` for nodes that may be targeted by
production reruns.

## LineagedResult focused audit

No blocking issue found. The audit covered the risky envelope boundaries:

- Users do not receive the envelope: `_with_current_run_context()` strips it
  before invoking user functions, and final returns use `_fetch_node_result()`.
- Hooks see user-facing values: `unwrap_result` receives unwrapped values, and
  hook replacements are reattached to lineage for downstream consumers.
- Explicit `NodeFuture` args and implicit `>>` args preserve lineage.
- Single-return tuple indexing and true multi-return tuple indexing preserve
  lineage on Local and Ray.
- `AppendResult` zero-copy is preserved while carrying lineage.
- Ray worker importability is handled by importable helper functions in
  `src/avalanche/_testing/rerun_helpers.py`, rather than silently xfail-ing Ray.

One audit gap was closed during this pass: added
`test/rerun_test.py:554`, which proves `unwrap_result` hook replacements do not
see `LineagedResult` but still preserve downstream row lineage on Local and Ray.

Residual risk is small and mostly combinatorial: unusual hook replacement shapes
combined with multi-return Ray paths could use more coverage later, but the main
transport boundaries are exercised.

## What looks good

- `ava.Rerun` is public, Pydantic-validated, `extra="forbid"`, and normalizes
  start slugs.
- Lazy vs autorun scheduler pruning works on Local and Ray.
- Rerun mode bypasses `ProgressStore` and does not corrupt ordinary stream state.
- Sparse overlay resolution walks rerun ancestry independently of filtered
  payload rows.
- Row-lineage fields are centralized and covered by backend-neutral schema tests.

## Remaining before merge

- Optional: document generated slug suffixes as public rerun-addressing behavior.
- Keep scan-mode backlog drain / Lance replay as a separate tracked follow-up.
