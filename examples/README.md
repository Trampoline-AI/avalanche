# Avalanche Examples

This directory contains the canonical examples. The standalone Python scripts
are covered by `test/example_smoke_test.py`.

Run the examples from this directory:

```bash
uv run ava dev .
```

This explicit target keeps discovery inside `examples/`. The local operator and
browser UI start together; select any discovered workflow in the UI to start a
run.

## Canonical Examples

### [`meeting_followups/`](meeting_followups/)

Five-node meeting workflow: load a transcript, extract unresolved follow-ups with
an agent, classify each item with TypeSafe, route by department in Python, and
optionally create real Linear issues. The included synthetic cross-functional
transcript contains repeated topics, corrections, rejected proposals, resolved
items, and explicitly unresolved ownership.

Run through the operator. From the repository root, with `OPENAI_API_KEY` and
`TYPESAFE_API_KEY` set in the operator environment:

```bash
uv run ava dev examples/meeting_followups/flow.py
```

In the browser, select `meeting_followups` and start a run—no workflow inputs or
uploads are required. The source step reads the bundled 2,238-word `meeting.txt`
relative to its module, not the working directory. Alternatively, submit a run
to the running operator from another terminal:

```bash
uv run ava run meeting_followups --connect localhost:7433
```

The command returns a run ID. Inspect the result in the browser or download it:

```bash
uv run ava result RUN_ID --connect localhost:7433 --wait \
  --output-dir .avalanche/meeting-followups-result
```

The default `PUBLISH = False` runs real extraction and classification but previews
the publication plan without contacting Linear. The run result contains the plan
and an empty receipts list. There are no canned AI answers or offline substitutes.
Agent defaults are `openai/gpt-5.4` and `openai/gpt-5.4-mini`; override them with
`MEETING_FOLLOWUPS_MODEL` and `MEETING_FOLLOWUPS_SUB_MODEL` in the operator
environment and supply the selected provider's credentials. The classifier uses
Avalanche's default `jev-latest`.

The transcript path, meeting title, date, and publication settings live in
`meeting_followups/config.py`. To use another local transcript, change those
settings. The extractor must preserve verbatim source passages and line ranges;
mismatched evidence stops the workflow. Semantic extraction accuracy still
requires review. The extraction signature lives in `signature.py`, while
`flow.py` contains only the operator-discovered nodes and graph.

The classifier asks independent `category` and `department` Choice questions per
item. Department selects the Linear team; category selects a label. Probabilities
remain available in classifier evidence but never gate routing. Department
responsibilities are defined in `schema.py`.

To publish, set a personal `LINEAR_API_KEY` in the operator environment and
configure `DESTINATIONS` in `config.py` with a `LinearDestination` for every
`Department`, including shared intake. Each destination contains:

- `team_id`: an actual Linear team UUID.
- `category_label_ids`: a mapping from `problem`, `request`, `proposal`, and
  `other` to actual label UUIDs available to that team.

Copy model UUIDs using Linear's command menu. The example does not create teams
or labels, guess account identities, or interpret informal deadlines as dates.
Explicitly stated owners and deadlines are preserved in issue descriptions;
issues are not automatically assigned to individual users. Linear uses the team's
default intake state (Triage when enabled, otherwise its first Backlog state).

Set `PUBLISH = True` in `config.py`, then start a new operator run. There is no
standalone Python runner or Avalanche input model.

Use demo teams: `PUBLISH = True` creates real issues. Successful output includes each
follow-up's issue identifier and URL. Publication is not transactional or
deduplicated across runs. On failure, confirmed issue URLs are attached to the
exception; the failed request may also have committed. Inspect Linear before
rerunning to avoid duplicates. Preview mode is explicit, not a fallback for errors.

### [`customer_feedback_review/`](customer_feedback_review/)

Production-shaped agentic review workflow. It analyzes a customer-feedback
workbook with parallel theme and risk agents, validates their reports
deterministically, then publishes an Excel product review and Word executive
brief. See its [README](customer_feedback_review/README.md) for the full flow.

### `complex_dag_pattern.py`

Simplest local flow execution path. It demonstrates the Python DAG API,
explicit data passing, fan-out, fan-in, and `ava.LocalExecutor` without requiring
Iceberg, Ray, the operator, or the TUI.


### `stream_pattern.py`

Stream-based incremental processing with local Iceberg tables. It uses the
current provider API:

```python
@ava.step
def chunk_documents(
    docs: pl.DataFrame = ava.Stream(
        ns.document, key="documents_to_chunks", mode="append_scan"
    ),
    *,
    dest=ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)
```

This example uses append-scan streams (`mode="append_scan"`): each consumer edge
gets a unique Stream `key`, and the runtime injects a `polars.DataFrame` for the
claimed snapshot. The default `ava.Stream(table)` is run-scoped and takes no key.


### `cursor_pattern.py`

Manual checkpoint control for advanced incremental flows. It demonstrates a
Cursor that tracks a source table snapshot while writing to a destination table,
model-specific cursors, and a multi-table sync checkpoint.


### `operator_workflow.py`

Flow file for the local operator and connected UI path. It is discovered along
with the other examples when `uv run ava dev` runs from this directory.

## Notes

- `ava.Stream(table)` is a provider marker, not an object you call with
  `.read()`. It defaults to run-scoped reads.
- For backlog/queue streaming use `ava.Stream(table, key="...", mode="append_scan")`
  with one unique key per consumer edge. `key` is only valid with `append_scan`.
- Local example artifacts are ignored by git through `.avalanche/`.
- The standalone scripts listed here are part of the smoke-tested onboarding path.
