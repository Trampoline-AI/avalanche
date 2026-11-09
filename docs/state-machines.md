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

@ava.machine(input=Request, key=("id",), key_format="{id}")
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
is a JSON array. `key_format` is optional and must reference exactly those
fields. `approval.spec` is a JSON-only declaration: states reference work graphs,
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

These are local authoring/test facilities, not restart durability or deployed
machine support. Ordinary workflow execution must not silently run an empty
machine carrier; use the machine test driver locally and a machine-capable host
for deployment. Remote instance routing and CLI machine commands use the host's public HTTP API.
Remote `machine.start` and `machine.send` preserve instance/event identities through
transient failures; see [remote HTTP retries](http-retries.md) for configuration.
