# Stream Reruns Implementation Review

## Current status

The current rerun implementation covers the identified stream-rerun gaps:

- rerun streams reject `row_lineage=False` before live `AppendResult` or
  DataFrame passthrough can short-circuit the read path;
- a declared workflow return pruned by rerun scheduling resolves to `None`,
  while scheduled return slots keep their normal values;
- multi-start lazy and autorun scheduling is covered on Local and Ray for sync
  and async node bodies, with one reused executor per executor test;
- skipped implicit non-stream inputs are rejected before Ray submission;
- sparse rerun-of-rerun ancestry uses the durable per-consumed-table property
  edge required when an intermediate lazy rerun wrote no rows to that table.

Payload and producer-version lineage remains in `_ava_rerun_of`,
`_ava_node_slug`, and `_ava_lineage_vector` row columns. The table property is
only the missing sparse ancestry edge; it is not a full manifest, stale bit, or
lineage hash.

## Focused evidence from this lane

The three regressions were run before implementation and failed as expected:

```text
FAILED ...requires_row_lineage_before_live_passthrough - DID NOT RAISE ValueError
FAILED ...single_declared_return_is_pruned - Workflow return node 'sink_1' was not scheduled by rerun
FAILED ...scheduled_tuple_return_and_none_for_pruned_return - Workflow return node 'sink_1' was not scheduled by rerun
3 failed
```

After the fixes:

- the same three regressions: `3 passed`;
- focused rerun selection, including the Local/Ray scheduler matrix:
  `10 passed, 1 warning`;
- final regression/matrix/DAG-return selection: `7 passed, 1 warning`;
- repository-wide Ruff check: `All checks passed!`;
- `make precommit-check`: `615 passed, 2 skipped, 71 warnings`.

## Contract notes

- Lazy reruns deliberately accept downstream staleness. Consumers can compare
  lineage vectors when freshness matters.
- Repeated default node invocations receive generated slugs `name`, `name_2`,
  and so on. Production-rerun targets should use explicit `slug=` values.
- `append_scan` backlog draining and Lance replay remain the explicitly deferred
  follow-up. Rerun mode bypasses `ProgressStore` and does not change that scope.
