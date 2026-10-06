# Inputs, context, and logging

```python
import avalanche as ava
```

## Input and context declarations

```python
class Request(ava.BaseInput):
    customer_id: str
    limit: int = 10

class Caller(ava.BaseContext):
    tenant: str
```

`BaseInput` and `BaseContext` are Pydantic model bases with no predefined fields. Subclasses declare their own required fields and defaults; construct them with keyword field values or pass mappings to [workflow.run](runs.md#workflow-run). Both allow arbitrary Python field types and forbid undeclared fields. They are not frozen or globally strict. Missing required/invalid/extra fields raise Pydantic validation errors.

Declare models with `@ava.workflow(input=Request, context=Caller)`. Input/context declarations must inherit the corresponding base (`TypeError` otherwise). Construction of a model does not start a run.

- Declared input: an existing instance is retained; other supplied data is validated against the model. Omission calls the model with no arguments, so required fields can fail. Without an input declaration, input is retained as supplied.
- Declared custom context: an existing instance is retained; otherwise the supplied value (or an empty mapping when omitted) is validated. Required custom fields therefore need caller values/defaults.
- Without a context declaration, supplied context is validated as `RunContext`. To carry arbitrary application fields, declare a `BaseContext` subclass; for unstructured metadata use `context={"metadata": {...}}`.
- A context subclass of `RunContext` combines application fields with runtime identifiers. Runtime-owned run/node fields are set by Avalanche, overriding caller values. Metadata remains caller-supplied.

See [Files and workspaces](files-workspaces.md) for file/tree fields in input models.

## Annotation injection

Author-facing node declaration:

```python
@ava.step
def inspect_request(request: Request, caller: Caller, run: ava.RunContext):
    return (request.customer_id, caller.tenant, run.run_id)
```

An unbound parameter annotated with a `BaseInput` subclass receives the run input **only when the validated input is an instance of that annotation**. A `BaseContext`-annotated parameter first receives compatible caller context; otherwise it receives compatible system `RunContext`. Thus a custom `Caller` and the system `RunContext` can both be injected into one node. Parameter names do not control injection.

Explicit keyword arguments override annotation injection, for example `inspect_request(request=other_request, caller=other_caller)`. A whole-input selector can also bind an input slot positionally, for example `consume(ava.input)` for `def consume(request: Request)`. Ordinary positional values do **not** override annotated input/context injection: passing `consume(other_request)` can leave the positional value in the call while also injecting `request` by keyword, causing a multiple-values `TypeError`. Use keyword overrides for input/context values.

Use resolvable, concrete class annotations: unions such as `Request | None` are not this injection contract. Merely annotating a node does not declare or validate workflow input; use the workflow's `input=` option. When no compatible value is available, the annotation supplies nothing; normal defaults/missing-argument errors apply. Use ordinary or keyword-only parameters for injected values.

### Run context

Injected `run: ava.RunContext` provides these author-facing fields (not a constructor reference):

| Field | Value |
| --- | --- |
| `run_id: str` | Current run identity, matching the run handle. |
| `workflow_name: str` | Workflow builder's name. |
| `executor_type: str` | Active executor label; ordinary local runs use `"local"`. |
| `node_id: str \| None` | Current invocation ID, `<function_name>_<count>`; numbering starts at 1 per workflow construction. |
| `node_name: str \| None` | Current node function name. |
| `node_slug: str \| None` | Invocation slug derived from the decorator's `slug` and repeated calls. |
| `metadata: dict[str, Any]` | Caller metadata, empty by default. |

Node fields are populated for node execution (otherwise initially `None`). Custom `BaseContext` subclasses do not automatically gain these fields; annotate a separate parameter with `RunContext` or inherit it. Compatible custom context takes precedence, including its metadata. System context used as a fallback is distinct from custom caller context and does not merge its fields/metadata. Context copies are not deep copies; do not assume metadata mutation is isolated between concurrent nodes.

## Input selectors

```python
ava.input
ava.input.customer_id
ava.input.customer.name
```

Pass these directly as node positional/keyword arguments inside a [workflow builder](workflows.md#node-calls-and-composition). They defer selection until the run input is validated: `ava.input` selects the whole object; dot attributes select nested attributes with `getattr`. Selectors are immutable and do not create executable nodes.

They do not support mapping-key lookup, subscripts, expressions, or recursive resolution inside argument containers. Raw dictionaries are not automatically converted to attribute namespaces. Missing underscore-prefixed attributes are unavailable, and existing selector attributes such as `path` are reserved rather than selectable input fields.

No available input raises `ValueError`; a missing attribute (including descent through `None`) raises `AttributeError` before the consuming node executes. Failures name the node, workflow, and selector. Use whole-input annotation injection to access fields that cannot be represented as dot selectors.

## Logger

```python
ava.Logger(name: str | None = None, level: int | None = None)

@ava.step
def report(log=ava.Logger()):
    log.info("Processing report")
```

`Logger` returns a declaration marker, not a live logger. Use it as a node parameter default or pass it explicitly as a keyword argument to the node call; an explicit keyword takes precedence over the default.

| Option/marker field | Contract |
| --- | --- |
| `name=None` | Underlying stdlib logger name; omitted/empty uses `avalanche.node.<node_name>`. |
| `level=None` | Leaves the logger's level unchanged. A supplied level calls `setLevel` on the shared stdlib logger; invalid values propagate stdlib logging errors. |

The injected object supports:

```python
log.debug(msg: str, **kwargs: Any) -> None
log.info(msg: str, **kwargs: Any) -> None
log.warning(msg: str, **kwargs: Any) -> None
log.error(msg: str, **kwargs: Any) -> None
log.critical(msg: str, **kwargs: Any) -> None
log.exception(msg: str, **kwargs: Any) -> None
```

Methods prefix available node/run/worker context, shortening run IDs and long non-local worker IDs to eight characters. They forward options such as `exc_info`, `extra`, and `stack_info` to stdlib logging; `exception` includes exception information. They do **not** accept stdlib positional formatting arguments: format the message first. Handlers/filtering remain stdlib logging configuration; the marker does not install a handler.
