# Workflows

```python
import avalanche as ava
```

## Primitive decorators

| Decorator | Purpose |
| --- | --- |
| `@ava.source` | Ingestion node. |
| `@ava.step` | Processing node. |
| `@ava.dest` | Export node. |
| `@ava.agent_step(...)` | Agent-backed node; declaration, options, and results: [Agents](agents.md#agent-step). |
| `@ava.classifier_step(...)` | Classifier-backed node; declaration, options, and results: [Classifiers](classifiers.md#classifier-step). |
| `@ava.workflow(...)` | Runnable graph declaration; options are listed [below](#workflow-declaration). |

For a direct agent invocation inside the workflow, use
[`ava.agent.step(...)`](agents.md#inline-call) rather than a decorated body.

The three ordinary node decorators have these signatures:

```python
ava.source(fn=None, *, num_returns=1, slug=None, retries=7, on_uncertain="retry")
ava.step(fn=None, *, num_returns=1, slug=None, retries=7, on_uncertain="retry")
ava.dest(fn=None, *, num_returns=1, slug=None, retries=7, on_uncertain="retry")
```

- `fn`: synchronous or asynchronous function to execute during a run. With no `fn`, returns a decorator; otherwise returns a callable node declaration. Decoration does not execute the function.
- `num_returns=1`: executor output count, not inferred from annotations. Counts greater than one declare multiple outputs, selected with indexing. Decoration does not validate the count or returned shape; invalid shapes can fail during execution.
- `slug=None`: invocation base name, defaulting to the function name. Repeated calls use the base slug, then `<slug>_2`, `<slug>_3`, etc. Conflicting explicit slugs or generated invocation slugs raise `ValueError` during construction.

Source/step/destination labels do not prohibit dependencies, return values, or side effects.
They share bounded `retries` and `on_uncertain` with agent steps; see
[Retries and waits](../retries-and-waits.md) for terminal/uncertain exceptions,
operation keys, graph waits and local controls. They accept no resource or executor
options. Ordinary Python argument and return annotations do not add runtime
validation; [input/context annotations](inputs-context.md) have a separate injection contract.

## Workflow declaration

Author-facing call form (selected options):

```python
@ava.workflow(
    cron=None, webhook=None, input=None, key=None, context=None,
    agent_defaults=None, classifier_defaults=None,
)
def flow():
    return produce() >> consume()

workflow = flow()
```

Bare `@ava.workflow` is also supported. It returns a builder; calling the builder returns a fresh workflow, without executing nodes. The declaration function must be synchronous and take no arguments.

| Option | Default and contract |
| --- | --- |
| `cron: str \| None` | `None`; scheduling expression. See [Triggers](#triggers). |
| `webhook: ava.Webhook \| bool \| None` | `None`; `True` enables a generated route, `False`/`None` disables it, or provide a `Webhook`. Other types raise `TypeError`. |
| `input: type \| None` | `None`; optional `BaseInput` subclass for run input validation. Other types raise `TypeError` on construction. |
| `key: tuple[str, ...] \| None` | `None`; opt into a permanent run identity derived from ordered input fields. Fields must exist and be unique; empty keys are invalid. `workflow.key(input)` validates those fields only. Hosts resolve the same run by ID or typed `--key field=value`, ignoring replacement non-key input/files on existing keys. Durable storage and CLI integration belong to the host; the embedded runner refuses keyed workflows. |
| `context: type \| None` | `None`; optional `BaseContext` subclass for caller context validation. Other types raise `TypeError` on construction. |
| `agent_defaults: Mapping[str, object] \| ava.agent.AgentDefaults \| None` | `None`; `{"runtime": ava.runtimes.PredictRLM(...)}`, the shared runtime each agent step's runtime is merged over. See [agent defaults](agents.md#workflow-defaults). |
| `classifier_defaults: dict[str, JsonValue] \| None` | `None`; validated workflow-level [classifier defaults](classifiers.md). |

Cycles raise `ValueError` during construction. Start execution with [workflow.run](runs.md#workflow-run); input and context declarations are detailed in [Inputs and context](inputs-context.md).

## Declared run starts

`ava.start_run(target, input=None, *, when=True)` declares a scheduler-owned
start for either a flow or machine; it never calls HTTP from worker code. A
builder/Workflow target compiles to its catalog name, or a name may arrive from a
node future. Input and `when` may also be futures. The output is `ava.RunStart`
(`run_id`, optional key, `created`, `read_state`), or `None` when skipped.
`when` must resolve to a strict boolean. False skips all other argument validation
and host effects, satisfies ordering edges, and does not implicitly skip children.
No Python conditional should branch on a future. Authoring tests register dynamic
targets with `workflow.test(targets=(...))`; the embedded runner refuses declared
start/send operations without a scheduler host. See [state machines](../state-machines.md)
for conditional routing, artifact admission and permanent-key retention policy.

## Node calls and composition

```python
value = produce(*args, **kwargs)
selected = value[0]
chain = produce() >> consume()
branches = left() & right()
joined = produce() >> (left() & right()) >> consume()
```

Calling a decorated node inside a workflow builder records one invocation and returns a deferred value. Outside a builder it raises `RuntimeError`. Calls do not execute the node or immediately validate its complete Python call signature. Every invocation is scheduled, including disconnected nodes and invocations not returned by the builder. Deferred values are not awaitable and have no result/cancellation methods; use the run handle.

| Operation | Contract |
| --- | --- |
| `node(*args, **kwargs)` | Records explicit arguments. A deferred value passed directly as a positional/keyword argument creates a dependency and resolves to its result. Containers of deferred arguments are not recursively wired. |
| `future[index: int]` | Selects an output or an element of an indexable single-return value without creating another executable node. Index/type/bounds errors surface during execution. |
| `left >> right` | Orders every left terminal before every right start. Returns a chain whose output is the right terminal output. |
| `left & right` | Groups branches in left-to-right order without adding dependency edges. Returns a parallel group usable on either side of `>>` or `&`. |

Operators accept only deferred node calls/chains and parallel groups (`TypeError` otherwise). Combining objects from different workflow constructions raises `RuntimeError`. Parenthesize parallel groups explicitly. Select multiple outputs with `value[0]` and `value[1]`, not iterable unpacking. Repeated indexing is not nested field access; index the specific node call, not a composite chain's terminal result.

### Argument binding

- `>>` supplies upstream data implicitly when the consumer has no explicit positional arguments or explicit upstream-provider selector. Parents already supplied as explicit deferred arguments are excluded from implicit values.
- With `LocalExecutor`, materialized implicit tuple/list outputs flatten one level into ordinary argument slots, and an implicit [AppendResult](storage/iceberg.md#append-result) supplies its `.data`. For explicit multi-slot binding, declare `num_returns=2` (or the required output count), then chain the outputs or pass explicit indexed arguments such as `consume(values[0], values[1])`. Explicit indexed arguments into a single-return container can fail when the result carries lineage metadata. Explicit unindexed deferred arguments preserve the whole result, including lists, tuples, and append results.
- Inline agent nodes preserve signature field boundaries instead of the ordinary-node unpacking rule above. A single list/tuple field remains one argument; multiple fields supply arguments in signature order.
- Input/context injections and non-upstream provider defaults do not consume ordinary upstream slots. [Stream](storage/streams.md) parameters consume their corresponding upstream positions. An explicit deferred argument for a stream-default parameter selects its producer rather than replacing the stream.
- Explicit positional arguments or explicit provider selectors suppress implicit chain-data binding, but preserve ordering. An ordinary explicit keyword that collides with an implicit positional value raises `TypeError`; values are not shifted to another slot.
- Missing, conflicting, or excess arguments fail at execution with ordinary Python/binding errors.

Independent branches may overlap; fan-in values follow declaration order, not completion order. See [LocalExecutor](runs.md#local-executor) for concurrency.

## Runtime fan-out with `map`

```python
node.map(*mapped, max_items: int | None = None, fail_fast: bool = True, **broadcast)
```

`map` is available on decorated nodes, including agent steps and destinations.
Each positional input must resolve to a list; inputs are zipped strictly.
Keyword arguments are broadcast to each item unchanged. For example:

```python
@ava.workflow
def articles():
    jobs = select_jobs()
    drafts = write_article.map(jobs, ws=workspace())
    edited = edit_article.map(jobs, drafts)
    return publish_batch(edited)
```

Here each article step handles one job, while `publish_batch` receives the full
ordered list. The returned `ava.MapFuture` is a deferred list value. Empty input
creates no children and resolves to `[]`. Gathering is argument resolution, not
another step execution. Maps also preserve their list when passed through `>>`.
An inline agent uses `ava.agent.step.map(Signature, *mapped, **broadcast)`; input
fields bind positionally in signature order, with `runtime=`, `tools=`, `skills=`,
`evaluations=` and `slug=` retaining their usual configuration meanings.

- The default maximum is `ava.DEFAULT_MAX_ITEMS = 8`; `max_items=` can raise it
  to `ava.HARD_CAP = 64`. Invalid limits fail during capture and name the step.
- A chained map inherits its sources' maximum and failure policy. Its own maximum
  can only lower that bound. Sources with different failure policies cannot be zipped.
- Unequal lengths fail with `map_length_mismatch`. Excess length fails with
  `map_overflow`, naming the step, actual length, maximum and `max_items=` hint.
  Inputs are never truncated.
- `fail_fast=True`: the first terminal item error fails the map and run, and stops
  admission. Queued futures are cancelled. The local executor **cannot interrupt
  already-running Python calls**; those calls finish before the run returns.
- `fail_fast=False`: every item runs to completion or failure before the map fails
  with `ava.MapItemsFailed`. Its `failures` contains each index, `child_id` and
  original exception. Successful children retain their ordinary outputs/evidence.
  Neither policy sends partial results downstream.

The deployment graph contains one group per map. A run creates children named
`<map_node_id>[i]` only when their inputs are ready. In a chain, item `i` can run
as soon as all mapped source items at `i` succeed and all plain list sources finish;
it does not wait for other indices. Run-scoped expansions are published before
any child lifecycle event, and do not mutate the captured workflow.

The browser graph and TUI show one group (`write_article ×4`), with actual item
status strips and per-item drill-down. Chained groups learn the full actual count
from root expansion: uncreated indices are UI-only `·` placeholders, not nodes or
failures. Before expansion the count is `×?`. Each real item uses the unchanged
per-node trace and evaluation views. A broadcast workspace uses the existing join
rules; write per-item paths to avoid conflicting edits.

Each local item uses its node's ordinary `retries` and `on_uncertain` policy.
Fail-fast and collect-all apply after that item's retry ladder is exhausted;
held items park without occupying a worker and retain their indexed identity
through reconciliation. `decision.map(show=fn)` runs its typed rendering steps
through the same map expansion and execution path. Local stream reruns
(`run(rerun=...)`) are not supported for map workflows. With execution services, a mapped managed input must first be returned
by a source step, rather than mapping a direct `ava.input` selector on the driver. Remote scheduling, durable retry generations and Delta integration are
separate from this local implementation. See the offline executable
[`map_agent_fanout.py`](../../examples/map_agent_fanout.py).

## Return contract

The workflow builder declares the value returned by `handle.result()` or `await handle`:

| Builder return | Executed result |
| --- | --- |
| `None` | `None`, after all scheduled nodes finish. |
| One deferred node call | Its resolved output; declared multiple outputs become a tuple. |
| Indexed deferred value | The selected element. |
| Chain | Its terminal output; a parallel terminal becomes a tuple in branch order. |
| Tuple | Immediate deferred entries resolve; other entries are retained unchanged, in order. |
| Any other value | Unchanged after execution. |

A bare parallel group, lists/dicts containing deferred values, and deferred values nested inside tuple entries are **not** recursively materialized. Return an explicit tuple of deferred values or a chain ending in a parallel group. Direct Python results need not satisfy CLI/remote output restrictions; see [Files and workspaces](files-workspaces.md).

## Triggers

`cron` and `webhook` are declarations, not timers or servers. A webhook becomes active when the workflow is discovered by the running [CLI operator](cli.md); a cron schedule also has to be enabled there (see below). Direct builder calls and `run()` do not activate them.

`cron` accepts a croniter-compatible expression, for example `"*/5 * * * *"`. Discovery validates it; workflow construction/direct runs do not. Scheduling checks local wall-clock minutes, at most once per workflow per matching minute. Scheduled runs start with the schedule's input (see below) and no context, so required context fields need usable defaults.

A discovered schedule starts **disabled**: the operator does not start cron runs
for it until you enable it. Enable or disable one workflow's schedule from the Web
UI workflow view, the TUI schedules panel (`Ctrl+S`), the REST API, or the CLI:

```bash
uv run ava schedule list
uv run ava schedule enable flows.py::nightly
uv run ava schedule disable flows.py::nightly
```

When the workflow declares an input model, enabling its schedule sets the input each
cron run starts with. In the Web UI, click the workflow's **inputs** dot to open the
input panel. It starts empty, or with the schedule's stored input, and shows each
field's default as placeholder text; only the values you set are sent. Run uses
them, and turning on the **Run on schedule** switch saves them for cron runs.
Starting a run or turning the schedule on is refused while a required field has no
value. The TUI opens its input form prefilled with the defaults; the CLI and REST
API take the input explicitly:

```bash
uv run ava schedule enable flows.py::nightly --input '{"region": "eu"}'
```

Without input, scheduled runs use the input model's defaults. The input is kept
while the schedule is disabled, so enabling it again reuses it, and replaced only
when you enable with new input. Like any run input, it is validated against the
input model when each run starts: an invalid input makes each scheduled run fail.

A disabled schedule starts no runs and reports no next run time. Once enabled, its
first run is the next cron occurrence after the minute you enabled it, even if the
current minute matches. A scheduled run that fails, for example because its stored
input is invalid, shows as a failed run with the Scheduled trigger in the run list;
enable the schedule again with corrected input to fix it. You can still run
the workflow by hand like any other. Whether a schedule is enabled, its input, and
the last minute it started a run are kept in the operator's state database
(`--state-db`) by workflow: they survive rediscovery, source reload, a changed cron
expression, and operator restarts, so a schedule that was on is on again after a
restart. A workflow that is no longer discovered keeps its schedule state until it
comes back. `ava operator --schedules-enabled` (or `ava dev --schedules-enabled`)
starts with every schedule enabled instead, for that start only: choices stored
earlier apply again the next time the operator starts without it.

Each run records what started it as `triggered_by`: `scheduled`, `manual` (the Web UI
or the TUI), `webhook`, or `api` (REST, gRPC, and `ava run` callers).

```python
ava.Webhook(path: str | None = None)
```

Returns immutable webhook configuration; `path` is its only field. `None` requests a generated route; an explicit path selects the local POST route. Route collisions prevent registration. A path must be printable, at most 1,024 characters, start with a single `/`, and contain no backslash, query, or fragment. Other than `/`, paths cannot have empty, `.` or `..` segments (including trailing/doubled slashes). Invalid paths raise `ValueError`.

The webhook listener is loopback-only by default. POST a JSON object with `Content-Type: application/json` and a valid `Content-Length`, at most 1 MiB; that object becomes the workflow input. Accepted requests return HTTP 202 with `{"run_id": "..."}`, not the completed result. Input/node failures can occur after acceptance. Invalid request framing/content and unsupported methods are rejected; unknown routes return 404, and failure to start a run returns 409. Configuration does not add authentication or expose a public deployment endpoint.
