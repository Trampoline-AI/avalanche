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

The three ordinary node decorators have these signatures:

```python
ava.source(fn=None, *, num_returns: int = 1, slug: str | None = None)
ava.step(fn=None, *, num_returns: int = 1, slug: str | None = None)
ava.dest(fn=None, *, num_returns: int = 1, slug: str | None = None)
```

- `fn`: synchronous or asynchronous function to execute during a run. With no `fn`, returns a decorator; otherwise returns a callable node declaration. Decoration does not execute the function.
- `num_returns=1`: executor output count, not inferred from annotations. Counts greater than one declare multiple outputs, selected with indexing. Decoration does not validate the count or returned shape; invalid shapes can fail during execution.
- `slug=None`: invocation base name, defaulting to the function name. Repeated calls use the base slug, then `<slug>_2`, `<slug>_3`, etc. Conflicting explicit slugs or generated invocation slugs raise `ValueError` during construction.

Source/step/destination labels do not prohibit dependencies, return values, or side effects. These decorators accept no retry, resource, or executor options. Ordinary Python argument and return annotations do not add runtime validation; [input/context annotations](inputs-context.md) have a separate injection contract.

## Workflow declaration

Author-facing call form (selected options):

```python
@ava.workflow(
    cron=None, webhook=None, input=None, context=None,
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
| `context: type \| None` | `None`; optional `BaseContext` subclass for caller context validation. Other types raise `TypeError` on construction. |
| `agent_defaults: dict[str, Any] \| None` | `None`; validated workflow-level [agent defaults](agents.md). |
| `classifier_defaults: dict[str, JsonValue] \| None` | `None`; validated workflow-level [classifier defaults](classifiers.md). |

Cycles raise `ValueError` during construction. Start execution with [workflow.run](runs.md#workflow-run); input and context declarations are detailed in [Inputs and context](inputs-context.md).

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
- Implicit tuple/list outputs flatten one level into ordinary argument slots. An implicit [AppendResult](storage/iceberg.md#append-result) supplies its `.data`. Explicit deferred arguments preserve the whole result, including lists, tuples, and append results.
- Input/context injections and non-upstream provider defaults do not consume ordinary upstream slots. [Stream](storage/streams.md) parameters consume their corresponding upstream positions. An explicit deferred argument for a stream-default parameter selects its producer rather than replacing the stream.
- Explicit positional arguments or explicit provider selectors suppress implicit chain-data binding, but preserve ordering. An ordinary explicit keyword that collides with an implicit positional value raises `TypeError`; values are not shifted to another slot.
- Missing, conflicting, or excess arguments fail at execution with ordinary Python/binding errors.

Independent branches may overlap; fan-in values follow declaration order, not completion order. See [LocalExecutor](runs.md#local-executor) for concurrency.

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

`cron` and `webhook` are declarations, not timers or servers. They become active when the workflow is discovered by the running [CLI operator](cli.md). Direct builder calls and `run()` do not activate them.

`cron` accepts a croniter-compatible expression, for example `"*/5 * * * *"`. Discovery validates it; workflow construction/direct runs do not. Scheduling checks local wall-clock minutes, at most once per workflow per matching minute, and starts runs without caller-supplied input/context. Required input/context fields therefore need usable defaults for scheduled runs.

```python
ava.Webhook(path: str | None = None)
```

Returns immutable webhook configuration; `path` is its only field. `None` requests a generated route; an explicit path selects the local POST route. Route collisions prevent registration. A path must be printable, at most 1,024 characters, start with a single `/`, and contain no backslash, query, or fragment. Other than `/`, paths cannot have empty, `.` or `..` segments (including trailing/doubled slashes). Invalid paths raise `ValueError`.

The webhook listener is loopback-only by default. POST a JSON object with `Content-Type: application/json` and a valid `Content-Length`, at most 1 MiB; that object becomes the workflow input. Accepted requests return HTTP 202 with `{"run_id": "..."}`, not the completed result. Input/node failures can occur after acceptance. Invalid request framing/content and unsupported methods are rejected; unknown routes return 404, and failure to start a run returns 409. Configuration does not add authentication or expose a public deployment endpoint.
