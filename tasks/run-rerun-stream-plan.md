# Stream Reruns — Two-Pager

## Problem and goal

Avalanche streams currently support two behaviors inside one primitive
(`ava.Stream` + `consume_stream`): zero-copy data passing from an upstream
`AppendResult`, and table-backed scan mode that claims pending snapshots via
`ProgressStore`. There is no way to rerun a past execution: when a step fails in
prod (or produced bad output), operators cannot say "re-execute step X for run R"
without hand-crafting a new run and fighting processed/unprocessed bookkeeping.

Goal: add **reruns** as a third mode on the existing stream primitive. A rerun is
triggered with a run ID plus starting points (steps identified by slug), and
executes against latest code (framework scope) or a pinned deployment (platform
scope, out of framework core). Rerun mode bypasses processed/unprocessed state
entirely: it fetches all rows written for the source run and reruns from the
given step(s). Two rerun behaviors: **autorun** (cascade through downstream DAG
dependencies, Marimo-style) and **lazy** (run only the named step; downstream left
as-is, for manual prod intervention).

Non-goal: full consistency enforcement. Lazy reruns may leave downstream stale;
the framework tolerates that (users are trusted adults) and only needs enough
lineage to *detect* staleness later, not prevent it.

## Proposed approach

**Mode selection.** Keep `Stream` a single marker; mode is a run-scoped property,
not a per-parameter one. `Workflow.run()` gains a rerun spec:

```python
wf.run(rerun=ava.Rerun(run_id="01J...", start=["chunk_docs"], mode="autorun"))
# mode: "autorun" | "lazy"
```

`RunContext` carries the rerun spec (new optional `rerun` field) so providers and
helpers can see it. New runs get a fresh `run_id`; produced rows encode the
source run in `_ava_rerun_of` (see below).

**Rerun read path.** In rerun mode `consume_stream` skips `ProgressStore`
entirely — no claim, no mark_done, no cursor. It reads rows for the source run
from the stream's table:

```
table.scan(filter=_ava_run_id == rerun.run_id)
```

This requires `row_lineage=True` tables; rerun mode on a table without lineage
columns is a hard error with a clear message. Snapshot-replay (`append_scan`) is
deliberately NOT used for reruns — lineage-column scan is mode 3's own read path,
decoupled from snapshot bookkeeping.

**Start points and slugs.** Steps are addressed by slug. Nodes have `fn.__name__`
(`node_name`), generated `node_id`, and stable `node_slug`. Slug defaults to
`node_name`, overridable via decorator (`@ava.step(slug="chunk-docs")`), validated
unique per workflow at build time. `Rerun.start` entries must match a slug or the
run fails fast at build/validation time, not mid-run.

**Autorun vs lazy scheduling.** The scheduler already has the dependency map
(`dependencies_map` in `dag.py`). Rerun planning is a graph-pruning step before
scheduling:

- `autorun`: execute the induced downstream closure of the start set; upstream
  nodes are not executed — their outputs are materialized from the lineage scan.
- `lazy`: execute only the start set; nothing downstream is scheduled.

Nodes not scheduled simply never submit to the executor. Start-set steps get
their `Stream` params fed from the rerun read path; explicit args from upstream
nodes that aren't rerun resolve from the lineage scan of *their* output tables
(v1 restriction: rerun start steps must consume via `Stream`; explicit-arg
dependencies on non-rerun upstream nodes are rejected at validation with a clear
error — lifts later if needed).

**Scan mode verification/fix (prerequisite).** Before adding mode 3, verify mode
2 works: known suspects are (a) `consume_stream` claims exactly one pending
snapshot per run, so backlogs never drain in one run; (b) `LanceTable.append_scan`
raises on multi-file snapshots. Write failing tests first (per CLAUDE.md TDD),
fix minimally: loop-claim until pending drained (bounded), and either fix or
explicitly document the Lance single-file limit.

**Lineage / staleness.** Ruled out: flipping staleness bits across the lake (too
expensive). Payload and producer-version lineage stays in row columns: rerun
rows carry `_ava_run_id`, `_ava_node_slug`, `_ava_rerun_of`, and a compact
`_ava_lineage_vector` identifying the actual producer versions consumed
(including mixed old/new upstreams). A sparse lazy rerun may consume a table
without writing any rows back to it, so row lineage on that table cannot encode
the ancestry edge without schema-invalid synthetic rows. Each consumed table
therefore stores one minimal durable property edge from the current rerun to its
source run. Rerun-of-rerun resolution follows that property edge, with row
columns retaining the payload/version facts. This property is not a full
manifest, stale bit, or lineage hash. Avalanche does not maintain fresh/stale
state in v1; consumers can compare lineage vectors when they need a freshness
view. No `_ava_lineage_hash` column in v1 unless cache indexing becomes a
concrete requirement.

## Risks and tradeoffs

- **Row-lineage dependency**: reruns only work on `row_lineage=True` tables.
  Acceptable: fail loud, document. Alternative (snapshot bookkeeping) re-couples
  reruns to the state we're explicitly bypassing.
- **Slug stability**: renaming a function breaks rerun addressing of old runs.
  Mitigation: explicit `slug=` override and docs guidance; not enforced in v1.
- **Explicit-arg upstreams in rerun**: v1 rejects rather than guessing
  materialization. Narrower but predictable.
- **Deployment pinning**: choosing code version is the operator/platform's job
  (Delta/Cirrus run-control); framework core only honors "current code". The
  `Rerun` spec carries `deployment_id: str | None` as an opaque pass-through for
  the operator so the wire contract doesn't change later.
- **Lazy staleness**: accepted by design; recorded via lineage pairing, not
  prevented.

## Execution plan

1. **run_id rename** — `execution_id` → `run_id` everywhere (RunContext,
   ParamContext, dag.py, lineage column `_ava_run_id`, operator/gRPC/CLI, docs,
   tests). No legacy aliases. Breaking for persisted lineage columns (alpha,
   accepted).
2. **Scan-mode verification** — **DEFERRED (out of scope for the rerun branch).**
   The rerun read path does not use scan-mode backlog claiming, so this
   prerequisite is split into a separate follow-up. `consume_stream` still claims
   one pending snapshot per call. Track the backlog-drain / Lance-replay fix
   independently of the rerun work.
3. **Slugs** — `slug=` on step/source/dest decorators, uniqueness validation at
   workflow build; tests in `test/` DAG suite.
4. **Rerun spec plumbing** — `ava.Rerun` model (Pydantic, `extra="forbid"`),
   `Workflow.run(rerun=...)`, `RunContext.rerun`, start-slug validation.
5. **Rerun read path** — `consume_stream` rerun branch (lineage-column scan, no
   ProgressStore); hard error on non-lineage tables.
6. **Scheduler pruning** — autorun closure / lazy start-set-only (multi-start
   supported) in `dag.py`; matrix tests: {autorun, lazy} × {Local, Ray} ×
   {sync, async} on the existing test patterns.
7. **Lineage vector clocks and sparse ancestry** — add `_ava_rerun_of`,
   `_ava_node_slug`, and `_ava_lineage_vector` to produced rows; record the
   minimal per-consumed-table rerun edge needed to resolve sparse chains. No
   full manifest, stale bit, or v1 lineage-hash column.
8. **Docs** — `docs/dag-api.md` section: three stream modes, rerun semantics,
   trusted-adult staleness stance.

Each phase is a separate commit; Codex review per phase, `make precommit-check`
gate before push.

## Definition of done

- Mode 2 (scan) verification is **deferred** to a separate follow-up (see
  execution item 2); it is not a gate for the rerun branch since the rerun read
  path does not depend on scan-mode backlog claiming.
- `wf.run(rerun=Rerun(run_id=..., start=[...], mode=...))` reruns from the named
  steps on Local and Ray, autorun cascades, lazy does not.
- Rerun mode ignores ProgressStore state both ways (doesn't read it, doesn't
  corrupt it) — asserted by test.
- New rows from a rerun carry `_ava_rerun_of` plus a lineage vector; sparse
  rerun ancestry is resolved with the minimal durable property edge on each
  consumed table.
- Full suite + lint green; docs updated.

## Decisions (resolved)

1. **Slug source**: default `fn.__name__`, optional `slug=` decorator override.
2. **Naming**: align on `run_id`. Rename `execution_id` → `run_id` across the
   framework: `RunContext.run_id`, `ParamContext`, `Workflow.run(run_id=...)`,
   lineage column `_ava_execution_id` → `_ava_run_id`, operator/gRPC/CLI wire
   fields, docs, tests. Alpha: clean rename, no legacy aliases or dual-read.
   Rerun keys on `run_id`; row-lineage rerun edge field is `_ava_rerun_of`.
3. **Lazy multi-start**: allowed in v1 — required for fanout intervention
   (rerun N parallel branches without cascading).
4. **Retry distinction**: reserve retry/retries for ordinary transient/error
   retry attempts (CAS conflicts, failed snapshot attempts, executor failures).
   The stream feature that intentionally re-executes prior run data is called a
   rerun everywhere in public API, docs, and lineage.
