# State-machine authoring and local tests

`@ava.machine` traces a builder once into a static, Pydantic `MachineSpec`.
It does not execute state work during discovery. Assign each `ava.state(work)`
to a local variable; that variable is the state's stable name. Work is an
ordinary step or workflow. Exactly one state is initial; final states have no
outgoing transitions. Each visit has at most 64 nodes, including bounded map
children.

```python
from datetime import timedelta
import avalanche as ava

class Request(ava.BaseInput):
    id: int

@ava.step
async def work() -> None:
    pass

@ava.machine(input=Request, key=("id",))
def approval():
    waiting = ava.state(work, initial=True)
    closed = ava.state(work, final=True)
    waiting.on("approved") >> closed
    waiting.after(timedelta(days=1)) >> closed

m = approval.test(Request(id=42))
assert m.state == "waiting"
m.send("approved")
assert m.state == "closed"
```

The key consists of the named input fields, in order; the wire representation
is a JSON array. Derive it with `approval.key(Request(id=42))`, which validates
against the input model. There is no separate formatted-key parser.
`approval.spec` is a JSON-only declaration: states reference work graphs,
transitions optionally declare a Pydantic payload schema, timers hold seconds,
and graph metadata carries retry policies, waits, maps and emissions.
Discovery rejects unknown targets, invalid initial/final states, undeclared
internal emissions and oversized visits. An event accepted by no state produces
a non-fatal discovery warning.

## Typed work events

A step declares `emits=("done", ...)` and returns
`ava.event("done", output)`, annotated `ava.Event[Output]`. A returned name not
in `emits` raises `NonRetryableError`, without spending retries on the same typo.
Downstream steps receive the typed output, not the event envelope. The machine
routes on the returned workflow event and carries its output into the next
state's unbound data parameter. The original machine input remains available
through `BaseInput` injection.

A workflow declares the same `emits` as its returned node. The returned node need
not be the last node to execute: the PR review example returns the review while
also waiting for `post_summary`. `@ava.workflow(output=Model)` validates the
workflow output (the event's data for an event-returning workflow).

`ava.Event` is also the base for typed decision answers. The previous decision
base was renamed without an alias. `OutcomeUnknownError` is different: an
external effect may have succeeded even though its reply was lost, so its name
and uncertainty policy are unchanged. Local visits reuse the existing decision
and reconcile controls; `m.decide(node, answer, reason=...)` resumes a parked
local decision without replaying verified work.

## Run-to-completion and external events

`state.on("changed", payload=Changed)` validates external payloads against that
model. Without `payload=`, the body must be empty. Events accepted by the current
state are buffered while its work runs, then consumed after the whole visit
finishes. Unlisted events are ignored. `defer=["changed"]` opts into buffering
for a later state. On transition the inbox drops events the new state neither
accepts nor defers. `m.waits.facts` exposes ignored/dropped local inbox facts.

A state's `after(timedelta(...))` timer starts on entry and is cancelled on exit.
`m.advance_time(duration)` advances a fake clock; no wall-clock sleep or worker
is needed. Exhausted step retries fail the instance. `on_uncertain="hold"`
continues to park for a human reconciliation rather than automatically replaying
an uncertain external effect.

## Test controls

The [PR lifecycle example](../examples/pr_lifecycle.py) exercises the review,
amend, review-again, merge and final-state history with
`stub={ai_review: [ava.event(...), ava.event(...)]}`. Other work actually runs.
A depleted stub fails rather than silently calling the real effect.

- `m.state`: current stable state name.
- `m.send(name, payload=None)`: a local external event; receipt reason is
  `accepted` or `ignored`.
- `m.history`: Pydantic entries with `state`, `event` and `source` (`internal`,
  `external`, `timer`, or `None` for the final entry).
- `m.advance_time(timedelta(...))`: fake-clock advancement.
- `m.handle`: the existing local run controls, receipt/idempotency boundary and
  terminal result/failure.

The test driver is native async and creates no threads. In async code, use
`m = await machine.test_async(...)`, `await m.asend(...)` and
`await m.aadvance_time(...)`. A synchronous `send` called by active state work
only queues the event; it never re-enters the driver.

## Declared machine operations

An instance is a run: starting it belongs to the normal run-start path,
`ava run <machine> --input ...`, with the same file/workspace inputs as flows.
The host derives the key from the input and returns the existing run for an
existing key. Local operator integration is separate work; this SDK change does
not yet make that path executable for machines.

Inside a workflow, `send` builds a graph node; it does not perform HTTP calls or
require credentials in worker code. Its arguments may be node futures. The
following test also uses the retained declared `start` node: how a router starts
an instance is pending the `ava-router-start` decision. Do not treat that node
as a settled router API.

```python
@ava.workflow
def router():
    started = approval.start(Request(id=43))
    sent = approval.send((43,), "approved")
    started >> sent
    return sent

run = router().test()
assert run.handle.result().status == "accepted"
```

`start` returns a node whose output is `ava.MachineStart(run_id, key, created)`.
`send` returns a node whose output is `ava.MachineEventReceipt`, containing
`receipt_id`, event `key`, `accepted_at`, `duplicate` and `status` (`accepted` or
`ignored`). Omitting a declared send's `idempotency_key` uses that node's stable
`RunContext.step_key`, so retries keep the same delivery identity. Defer/drop
remain later inbox facts, not additional receipt statuses.

`workflow.test()` and `await workflow.test_async()` execute these operations
against an isolated in-memory host, sharing the machine transition reducer.
These are authoring/test facilities, not restart durability. `workflow.run()`
refuses MACHINE nodes with a missing-host error; operator-host execution is a
separate integration. `machine.run()` is not an instance execution API.

Outside a workflow, send to an already started instance with an explicit host:

```python
async def approve(host: ava.MachineHost):
    return await approval.send(
        approval.key(Request(id=44)), "approved",
        idempotency_key="delivery-44", host=host,
    )
```

The host implements async `send(machine, key, event, payload, idempotency_key)`;
its `start(machine, key, input)` hook remains only for the retained declared
start node. There is no explicit-host `Machine.start` API. The standalone SDK
does not select a host, read hosted environment settings, or ship a hosted HTTP
client. This breaks 0.20.x callers using API URL/token/organization/project,
deployment or retry transport kwargs: start through the run path and send with
`host=`. Outside a workflow, sends must provide their own replay identity.
Transport retry policy belongs to the explicit host; see
[host retries](http-retries.md).

The decorator and discovery spec no longer accept formatted-key templates.
Remove that argument from 0.20.x declarations. Hosts address instances by run ID
or named key fields parsed with the ordinary flow input-field parsing; the SDK
only derives keys from validated input with `Machine.key(input)`.
