# Runs

```python
import avalanche as ava
```

## Workflow run

Author-facing call form (selected parameters, shown by keyword):

```python
handle = workflow.run(
    executor=None,
    input=None,
    context=None,
    run_id=None,
)
```

Starts execution and returns a process-local `RunHandle` without waiting for completion. Obtain the workflow from a [workflow builder](workflows.md#workflow-declaration). Repeated/concurrent calls create independent executions.

| Parameter | Contract |
| --- | --- |
| `executor=None` | Default selection uses Ray when importable, otherwise local execution. Choose `ava.LocalExecutor()` explicitly for predictable in-process runs. Ray initialization/execution failures do not silently fall back to local. |
| `input: Any = None` | Validated against the declared `BaseInput` subclass. An existing instance is retained; other values are validated; omission constructs the declared model with no arguments. Without an input declaration, retained as supplied. |
| `context: Any = None` | Caller context validated against the declared `BaseContext` subclass, or `RunContext` when undeclared. Runtime-owned identifiers cannot be overridden. See [injection and context fields](inputs-context.md). |
| `run_id: str \| None = None` | Caller identity or generated ULID string; available as `handle.run_id` and in injected context. Does not deduplicate runs or provide persistent lookup. |

Executor setup can raise before a handle is returned. Input/context validation, argument binding, provider preparation, and node errors ordinarily surface when waiting on the handle. The driver runs in a non-daemon background thread; returning a handle does not imply the process can exit before execution finishes.

## Run handle

Obtain this object from `workflow.run()`, not by manual construction. Its public `run_id: str` identifies this run. Successful output follows the [workflow return contract](workflows.md#return-contract).

| Call | Return and behavior |
| --- | --- |
| `handle.result(timeout: float \| None = None)` | Waits for and returns the cached output, or re-raises the execution failure. Timeout is seconds; `None` waits indefinitely. |
| `handle.exception(timeout: float \| None = None) -> BaseException \| None` | Waits and returns the cached failure, or `None` on success. |
| `await handle` | Asynchronously waits for the same output/failure without blocking the event loop. |
| `handle.running() -> bool` | Execution started and has no terminal outcome. |
| `handle.done() -> bool` | Success, failure, or cancellation is terminal. |
| `handle.cancel() -> bool` | Requests cooperative cancellation; `True` for a new request, `False` if already requested or terminal. |
| `handle.cancel_requested() -> bool` | A cancellation request has been made/observed. |
| `handle.cancelled() -> bool` | Cancellation became the terminal outcome, not merely requested. |

Timed waits raise `concurrent.futures.TimeoutError`; `result()` and `exception()` raise `concurrent.futures.CancelledError` for a cancelled run. Timeouts do not cancel work. Repeated waits reuse the outcome.

Cancellation stops future admissions when observed and drains already-submitted work; it does not kill an active function. A last node may finish before cancellation is observed, so a requested run may still succeed. Cancelling an asyncio task awaiting the handle cancels that waiter, not the run: call `handle.cancel()` explicitly. Cancelling one run does not cancel another run of the same workflow.

## Local executor

```python
ava.LocalExecutor(*, max_workers: int | None = None)
```

Returns an in-process executor for `workflow.run(executor=...)`. `max_workers=None` uses the scheduler's default thread-pool size; a positive integer bounds concurrent dependency-ready nodes. `1` completes one node before admitting the next and checks cancellation between nodes. Non-integers (including `bool`) raise `TypeError`; zero/negative values raise `ValueError`.

Both synchronous and asynchronous node bodies complete before their outputs are available to dependents. Independent branches can overlap unless `max_workers=1`; completion order is unspecified. Already-running concurrent work may finish after cancellation/failure. Synchronize shared mutable application state yourself. [Workspace](files-workspaces.md#workspace) inputs and outputs retain their per-invocation isolation and snapshot semantics.
