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

Seven-node meeting workflow: load a transcript, extract unresolved follow-ups with
an agent, classify each item with TypeSafe, then route separate lists to three
parallel demo destination steps named Linear, Attio, and Jira. The included
synthetic cross-functional transcript contains repeated topics, corrections, rejected
proposals, resolved items, and explicitly unresolved ownership.

Run through the operator. From the repository root, with model credentials (see
[Model selection](#model-selection)) and `TYPESAFE_API_KEY` set in the operator
environment:

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

The workflow runs real extraction and classification, then returns three demo plans
in Linear, Attio, and Jira order. Each plan names its destination and contains the
routed issues, including an empty list for unused destinations. These service names
are illustrative: the example has no clients, credentials, or live publishing mode
for them. There are no canned AI answers or offline substitutes.
Agent models come from [model selection](#model-selection); override them with
`MEETING_FOLLOWUPS_MODEL` and `MEETING_FOLLOWUPS_SUB_MODEL` in the operator
environment and supply the selected provider's credentials. The classifier uses
Avalanche's default `jev-latest`.

The transcript path, meeting title, date, and destination routing live in
`meeting_followups/config.py`. To use another local transcript, change those
settings. The extractor must preserve verbatim source passages and line ranges;
mismatched evidence stops the workflow. Semantic extraction accuracy still
requires review. The extraction signature lives in `signature.py`, while
`flow.py` contains only the operator-discovered nodes and graph.

The classifier asks independent `category` and `department` Choice questions per
item. Department selects the destination through `DESTINATIONS` in `config.py`:

| Department | Destination |
| --- | --- |
| Engineering | Linear |
| Product | Jira |
| Marketing | Attio |
| Support | Attio |
| Shared intake | Linear |

Every department must have a mapping. Departments sharing a destination are combined
into one list; no follow-up is dropped. Category and source evidence are preserved
in each planned issue. Probabilities remain available in
classifier evidence but never gate routing. Department responsibilities live in `schema.py`.

Explicitly stated owners and deadlines remain in the planned issue descriptions.
No issues or tasks are created in external services.

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

## Model selection

The agent examples (`meeting_followups/`, `customer_feedback_review/`, and
`evaluations_workflow.py`) do not hard-code a model provider. When an example loads,
[`model_selection.py`](model_selection.py) checks what is set up on the machine and
picks the first available option, in this order:

| Order | Provider | Selected when | Model |
| --- | --- | --- | --- |
| 1 | Codex LM | A `codex-lm` login is found | `gpt-5.6-terra` |
| 2 | OpenAI | `OPENAI_API_KEY` is set | `openai/gpt-5.6-terra` |
| 3 | Anthropic | `ANTHROPIC_API_KEY` is set | `anthropic/claude-sonnet-5-5` |

Each option uses the same model for the main agent (`lm`) and its sub-model
(`sub_lm`). If both API keys are set and Codex LM is not, OpenAI wins. The operator
UI shows Codex LM models as `codex/gpt-5.6-terra`, so you can tell them apart from
the OpenAI API.

**Codex LM detection.** Codex LM uses a ChatGPT subscription instead of an API key.
The example asks `codex-lm` to resolve its auth profile exactly as a model call
would: the `CODEX_LM_AUTH_PROFILE` override, saved-profile rotation, or the active
profile. It counts as set up when that profile's `auth.json` exists. The Codex CLI's
own `~/.codex/auth.json` counts only when `CODEX_LM_ENABLE_LEGACY_AUTH_FALLBACK=1`.
To create a profile:

```bash
uv run codex-lm auth login NAME
```

**API keys.** Keys can be exported or placed in the project `.env`. The example loads
`.env` without overriding values already in the environment, as classifier steps do.

**Nothing set up.** Codex LM stays selected. The example still loads, so the operator
can discover it, but the first agent call fails with Codex LM's login instructions.
Classifier steps and evaluations separately need `TYPESAFE_API_KEY`.

**Overrides.** Per-example variables replace the automatic choice. Give any LiteLLM
model ID and set that provider's credentials:

| Example | Main model | Sub-model |
| --- | --- | --- |
| `meeting_followups/` | `MEETING_FOLLOWUPS_MODEL` | `MEETING_FOLLOWUPS_SUB_MODEL` |
| `customer_feedback_review/` | `CUSTOMER_FEEDBACK_REVIEW_MODEL` | `CUSTOMER_FEEDBACK_REVIEW_SUB_MODEL` |

`evaluations_workflow.py` has no override variables. To use a different provider for
it, unset the higher-priority credentials.

Each run imports the example in a fresh process, so selection happens again for every
run. A new Codex LM login or `.env` change applies to the next run. Exported variables
come from the operator's environment, so restart the operator after changing them. To
see which provider will be picked, run from `examples/`:

```bash
uv run python -c "from model_selection import select_models; print(select_models().provider)"
```
