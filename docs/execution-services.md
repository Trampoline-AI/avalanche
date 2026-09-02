# Execution services

Execution services let a platform materialize worker-visible inputs, open task-scoped
resources, and publish a small commit receipt around each Avalanche node invocation.
They keep storage credentials, durable snapshot identity, and platform lifecycle state
out of workflow code.

This is an advanced platform integration API. Most workflow authors only declare typed
input and use the paths or capabilities delivered by their platform.

## User experience

Workflow code stays ordinary Avalanche code:

```python
import avalanche as ava


class DocumentInput(ava.BaseInput):
    source_path: str


@ava.source
def read_document(payload: DocumentInput) -> str:
    with open(payload.source_path, encoding="utf-8") as source:
        return source.read()


@ava.workflow(input=DocumentInput)
def document_flow():
    return read_document()
```

A platform starts the same workflow with an execution-services request:

```python
handle = document_flow().run(
    executor=ava.RayExecutor(),
    input={"source_path": {"artifact_id": "artifact_123"}},
    execution_services=ava.ExecutionServicesSpec(
        service=platform_services,
        request=platform_request,
    ),
)

result = handle.result()
receipts = handle.execution_receipts()
```

The raw `input` may contain immutable platform descriptors that are not valid
`DocumentInput` values on the driver. The service materializes those descriptors in the
actual worker and returns a valid `DocumentInput` or mapping before the task runs.

An `ava.input.<field>` selector resolves against that same worker-materialized input:

```python
@ava.source
def read_path(source_path: str) -> str:
    with open(source_path, encoding="utf-8") as source:
        return source.read()


@ava.workflow(input=DocumentInput)
def selected_document_flow():
    return read_path(ava.input.source_path)
```

Without `execution_services=`, input validation and execution retain their ordinary
behavior. `handle.execution_receipts()` returns an empty tuple.

## Materialization semantics

`materialize_input` means “make the declared task input available through the worker.”
It does not require an eager byte copy.

A provider may:

- eagerly download immutable objects into an attempt directory;
- return paths in an attempt-local lazy filesystem that fetches bytes on first access;
- bind task-local output reservations or other capabilities while constructing input;
- validate the returned value directly as the workflow's `BaseInput` type.

The returned value must expose every path and value required by the task. A lazy
filesystem may defer backing-byte fetches until those paths are opened.

## Architecture

Execution services surround the actual user task:

```text
Driver
  |
  | Workflow.run(execution_services=spec)
  v
LocalExecutor or RayExecutor
  |
  | submit service request + task identity + parent receipts
  v
Actual worker
  probe
    -> negotiate
    -> open
    -> materialize_input
    -> resolve ava.input selectors and typed input injection
    -> run user task
    -> normalize declared return values
    -> finalize
    -> teardown
```

The lifecycle is provider-neutral. Avalanche owns when methods run and how their values
move through the DAG. The provider owns resource allocation, materialization,
publication, and cleanup.

### Public types

`ava.ExecutionServices` is the provider protocol. It defines:

```python
class ExecutionServices(Protocol):
    def probe(self, *, request, task): ...
    def negotiate(self, *, request, task, probe): ...
    def open(self, *, request, task, negotiation, upstream_receipts): ...
    def materialize_input(self, *, session, input_type, input): ...
    def finalize(self, *, session): ...
    def abort(self, *, session, error): ...
    def teardown(self, *, session): ...
```

Provider lifecycle methods are synchronous. `ExecutionServicesSpec` rejects declared
async methods, and runtime invocation rejects a synchronous wrapper that returns an
awaitable. Workflow task functions may still be synchronous or asynchronous.

`ava.ExecutionServicesSpec` combines a serializable provider with an immutable request.
Its version is currently `avalanche.execution-services/v1`.

`ava.ExecutionTaskSpec` identifies the consuming task with:

- run ID;
- workflow name;
- node ID and function name;
- stable node slug;
- executor type (`local` or `ray`).

`ava.ExecutionServiceReceipt` exposes one deterministic terminal receipt through the
run handle. It contains the terminal node ID, node slug, and provider-returned value.

### Request and session ownership

The request crosses the executor boundary. It must be immutable and serializable, and
must not contain:

- credentials or secret values;
- user-facing storage URIs;
- absolute worker-local paths;
- open files, sockets, or other process-local handles;
- actors, placement handles, or scheduler-affinity tokens.

The provider acquires credentials and process-local capabilities after the executor has
placed the task. `open` returns the task-scoped session used by the remaining lifecycle
methods. Sessions never cross from one task attempt to another.

### Local and Ray execution

`LocalExecutor` runs each node's lifecycle synchronously within that node's
worker thread and carries values directly. Independent node lifecycles may
overlap, while a dependent lifecycle receives completed parent receipts in DAG
order.

`RayExecutor` runs the complete lifecycle inside one Ray task. The task returns three
separate channels: user payloads, one small receipt, and one status marker. The driver
fetches status markers to observe progress and failure. Receipt references remain in the
DAG as worker-side dependencies and are fetched only for terminal publication, so
intermediate receipts and user payloads are not materialized on the driver.

For a fan-in node, `open(..., upstream_receipts=...)` receives parent receipts in DAG
dependency order. Only terminal-node receipts are fetched and exposed through
`RunHandle.execution_receipts()`.

## Failure and cleanup contract

The lifecycle is strict:

- `probe`, `negotiate`, or `open` failure stops execution; there is no fallback;
- after `open` succeeds, any materialization, user-task, return-normalization, or
  finalization failure calls `abort`;
- `teardown` runs exactly once after every opened session;
- successful finalization is not followed by `abort`;
- a finalization failure requests `abort`; providers may preserve explicit recovery state
  when another destructive cleanup attempt would be unsafe;
- abort or teardown failures are attached as exception notes and never mask the primary
  materialization, task, normalization, or finalization error;
- teardown failure after an otherwise successful lifecycle fails the task;
- failure or cancellation propagates to both `result()` and `execution_receipts()`.

If an executor retries a task, the entire lifecycle starts again with a new session.
Avalanche does not reuse a partially opened or finalized session. Providers should make
requests idempotent and use attempt-specific ownership or winner fencing where multiple
attempts can race.

Cancellation is cooperative between node submissions. An already running Local or Ray
task may finish its lifecycle. Completed receipts remain valid, while unscheduled
downstream tasks do not open sessions.

## Provider implementation checklist

Before enabling a provider, verify that it:

1. serializes its service and request without credentials or local handles;
2. negotiates an explicit supported mode and fails closed otherwise;
3. acquires worker-local capabilities only after scheduling;
4. returns the declared `BaseInput` type or a mapping Pydantic can validate;
5. supports `ava.input` selectors, scalar/list/optional/empty values, and fan-in;
6. keeps user payloads, receipts, and status markers on separate channels;
7. aborts partial publication or preserves explicit recovery state, then tears down every
   opened session exactly once;
8. restarts cleanly under retries and preserves winner fencing;
9. behaves equivalently under Local and Ray execution;
10. documents whether its materialization is eager or lazy.

## Current boundary

Execution services do not define a storage model, credential system, retry budget,
retention policy, or shared filesystem. Those remain platform responsibilities.
Avalanche provides the worker lifecycle and DAG control channel needed to implement them
without exposing platform state to workflow code.
