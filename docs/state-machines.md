# State-machine authoring and local tests

`@ava.machine` traces a builder once into a static, Pydantic `MachineSpec`.
It does not execute state work during discovery. Assign each `ava.state(work)`
to a local variable; that variable is the state's stable name. Work is an
ordinary step or workflow. Exactly one state is initial; final states have no
outgoing transitions. Each state entry has at most 64 nodes, including bounded map
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
only those key fields (full input validation belongs to a new run). There is no separate formatted-key parser.
`approval.spec` is a JSON-only declaration: states reference work graphs,
transitions optionally declare a Pydantic payload schema, timers hold seconds,
and graph metadata carries retry policies, waits, maps and emissions.
Discovery rejects unknown targets, invalid initial/final states, undeclared
internal emissions and oversized state entries. An event accepted by no state produces
a non-fatal discovery warning.

## Keys are a run feature

Ordinary workflows can opt into the same permanent run identity:

```python
@ava.workflow(input=Request, key=("id",))
def notification():
    return work()

assert notification().key({"id": 42}) == (42,)
```

The ordered typed fields and key derivation are identical to machines. Hosts
start both through the normal run path and resolve run IDs or named
`--key field=value` arguments with the ordinary input-field parser. Existing
keys return the original run and ignore replacement non-key input/files; omitted
`key=` keeps independent ordinary flow starts. Discovery exports `key_fields` for
both kinds. The embedded runner refuses keyed workflows because it has no durable
identity index; use `.test()` for isolated authoring or a run-capable host such
as `ava operator`.

Key fields follow one portable contract (`avalanche.run_keys`) on the test host,
the local operator and deployed hosts: `str`, `int`, `float`, `bool`, `None`,
`UUID`, `date`, UTC `datetime`, enums (by value), tuples, sets and frozensets
(sorted canonically, independent of the hash seed), plain nested models and
plain unions or `Optional`. Existing list, dict and literal key shapes keep
their bytes. The decorator refuses a key field with custom validators or
serializers, aliases or alias generators, default factories, or an unsupported
type or model configuration, and the error names the field. Normalize business
values before the start and pass a plain identity field instead.

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
and uncertainty policy are unchanged. Local state entries reuse the existing decision
and reconcile controls; `m.decide(node, answer, reason=...)` resumes a parked
local decision without replaying verified work.

## Run-to-completion and external events

`state.on("changed", payload=Changed)` validates external payloads against that
model. Without `payload=`, the body must be empty. Events accepted by the current
state are buffered while its work runs, then consumed after the whole state entry
finishes. Unlisted events are ignored. `defer=["changed"]` opts into buffering
for a later state. On transition the inbox drops events the new state neither
accepts nor defers. `m.waits.facts` exposes ignored/dropped local inbox facts.

A state's `after(timedelta(...))` timer starts on entry and is cancelled on exit.
`m.advance_time(duration)` advances a fake clock; no wall-clock sleep or worker
is needed. Exhausted step retries fail the instance. `on_uncertain="hold"`
continues to park for a human reconciliation rather than automatically replaying
an uncertain external effect.

## Artifact payloads, replay and retention

Event payload models may declare `File`, `Workspace`, lists or nested models,
just like run inputs. Send preserves native models until host admission; dumping
JSON references alone does not upload, authorize, copy or retain their contents.
The host must use the normal upload/reference path and bind accepted payloads
into the target run before issuing a receipt, including deferred events. A host
that has not implemented uploads must explicitly refuse them, never accept an
unusable reference. The in-memory test host can bind locally registered references;
it is not durable artifact storage.

The local operator passes File and Workspace values to another run, as child
input or in an event, by reference: the recipient binds the same file or folder,
and nothing is restaged, exported or copied. Local references are not snapshots;
the file or folder must stay on disk, as for flows. A replayed start or send
binds nothing new. Event payloads, including deferred ones, are validated
against the receiving model before acceptance; a missing reference or a
mismatched file digest is rejected.

An existing key is resolved after authorization and validation of key fields only.
Replacement input/files are ignored, not retained or applied; only a new key needs
complete input/artifact validation. General machine reruns, from-step reruns and
from-state-entry reruns are unsupported. Interrupted-state-entry repair is a new state entry in the
same state/run, not replay. A whole replay requires a different business key.
Key-to-run identity is permanent even when terminal evidence expires under the
host's normal retention policy: reads return `read_state="expired"`, not an empty
history masquerading as success. Live inputs and deferred payloads must remain
reachable for the life of their run. No fixed retention duration is promised.

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
existing key. The local operator executes machines through this same run path;
see [Run locally](#run-locally).

Inside a workflow, `ava.start_run(target, input)` starts either a flow or a
machine through the same declared run operation. `machine.send` declares the
only machine-specific operation. Neither performs worker HTTP calls or requires
worker credentials. Arguments, including a catalog-name target, may be node futures.

```python
@ava.workflow
def router():
    started = ava.start_run(approval, Request(id=43))
    sent = approval.send((43,), "approved")
    started >> sent
    return sent

run = router().test()
assert run.handle.result().status == "accepted"
```

`ava.call(target, input, *, timeout=None, when=True)` declares a start plus a
workerless wait and returns the child's typed final output. It is a graph
operation, **not** an awaited function. A state entry can call a flow or another
machine, including a different key of its own definition. A machine call waits
until the instance closes; its queued events continue to follow ordinary inbox
rules. The target object is captured by name without eagerly building its graph.

```python
from datetime import timedelta

@ava.workflow
def review_and_wait():
    verdict = ava.call(approval, Request(id=45), timeout=timedelta(days=7))
    return verdict
```

The returned future can feed ordinary steps or be a workflow's return value.
`when=False` returns `None` without starting or waiting. Calls propagate
`child_run_failed`, `child_run_cancelled`, or `child_run_timeout` with the child
run ID and stop dependent work. Timeout leaves the child running. Self-instance
waits (`child_run_self_wait`) and wait cycles (`child_run_wait_cycle`) are refused.
Cancelling the caller cancels only runs it created, not keyed runs it attached
to. The local operator stores waits and links in SQLite, restores parked callers
after restart (a crash or SIGINT/SIGTERM; stopping the operator cancels
nothing), and exposes both link directions with `ava run links RUN_ID --json`
and `GET /api/v1/runs/RUN_ID/links`. A child still executing at restart fails,
and its caller receives that failure. The operator UI shows the run tree (see
[Operator UI](#operator-ui)). Delta runs the same calls.

`ava.start_run` returns `ava.RunStart(run_id, key, created, read_state)`. An
unkeyed flow has `key=None`; a permanent key may resolve to an `expired` run
whose terminal history/artifacts have been removed by normal retention.
`send` returns a node whose output is `ava.MachineEventReceipt`, containing
`receipt_id`, event `key`, `accepted_at`, `duplicate` and `status` (`accepted` or
`ignored`). Omitting a declared send's `idempotency_key` uses that node's stable
`RunContext.step_key`, so retries keep the same delivery identity. Defer/drop
remain later inbox facts, not additional receipt statuses.

Both operations accept `when=bool_or_future`. The scheduler first resolves a
strict boolean: false skips other argument validation and host calls and yields
`None`. It satisfies ordering edges but does not implicitly skip children. A
consumer of a conditional result must handle `None`; separate predicates express
start vs send (and false/false for irrelevant deliveries). This is not Python
branching on a future. The [local router example](../examples/pr_lifecycle.py)
uses two explicit predicates. For dynamic catalog-name targets, register test
implementations with `workflow.test(targets=(flow_builder, machine, ...))`.

`workflow.test()` and `await workflow.test_async()` execute these operations
against an isolated in-memory host, sharing the machine transition reducer.
These are authoring/test facilities, not restart durability. The operator parks
RUN and MACHINE nodes and performs their operations on its host; no step executor
or tenant credential is used. Bare embedded `workflow.run()` still refuses these
nodes without that host. `machine.run()` is not an instance execution API.

Outside a workflow, send to an already started instance with an explicit host:

```python
async def approve(host: ava.MachineHost):
    return await approval.send(
        approval.key(Request(id=44)), "approved",
        idempotency_key="delivery-44", host=host,
    )
```

The host implements async `send(machine, key_or_run_id, event, payload, idempotency_key)`
only. `Machine.start`, `MachineStart` and a host-start API do not exist. The
scheduler handles declared starts through its ordinary run path. The standalone SDK
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

## Run locally

Save the [approval example](../examples/approval.py) as `approval.py`. Its input
is `{"ticket":44}`; an `approved` event carries `{"reviewer":"Ada"}`. It runs
`pending -> cooling -> done`, with a two-second real timer in `cooling`.

```sh
ava operator approval.py
# In another terminal, using the same operator:
ava run approval --input '{"ticket":44}'
# Prints a run ID. Repeating this command returns that same run.
ava machine send approval --key ticket=44 approved \
  --data '{"reviewer":"Ada"}' --idempotency-key approval-44
ava flow list
ava flow show approval           # `ava flow get` is equivalent
ava run list --flow approval
ava run show RUN_ID
ava run history RUN_ID --limit 20
ava result RUN_ID --wait --output-dir ./approved-result
```

### Read run, state entry and step outputs

```sh
ava run result RUN_ID --json                       # final output, after closure
ava run result RUN_ID --entry 1 --json              # completed entry's return
ava run result RUN_ID --entry 1 --node inspect_1 --json
```

The same commands read ordinary flows; omit `--entry` for a flow step.
`ava result` is equivalent to `ava run result`. `--wait` waits for the selected
result, so reading a completed state entry does not wait for the machine to close.
Without `--json`, use `--output-dir PATH` to explicitly download artifacts.

Operator REST serves the same document at
`GET /api/v1/runs/RUN_ID/result?entry=1&node_id=inspect_1`.
The document has `run_id`, nullable `entry` and `node_id`, `version: 1`, and
`value` in the shared result codec. Models use JSON aliases; tuples have a tuple
kind; sets are canonical ordered lists. File and Workspace values carry references,
not inline bytes. A successful null return is a scalar null value; a missing,
unfinished or failed result is an error, never a fabricated null.

Completed state entry/step outputs survive an operator restart and expire with the
run's retained details. Outputs discarded by older operator versions cannot be
reconstructed from display previews.

Delta uses `delta runs result RUN_ID --json` with the same selectors and shape,
and `GET /v1/orgs/ORG/projects/PROJECT/runs/RUN_ID/result`. A flow deployment
must carry its explicit return declaration: redeploy older flows to enable final
run-result reads. Existing per-step `/outputs` reads remain available. Machine
state entry results stay readable after state-entry compaction through bounded archive reads.

Use `ava machine send approved --run RUN_ID` when the run ID is available;
this form omits the machine name because the run already identifies it.
`--key` is repeated for multiple key fields. Values use the ordinary flow-input
JSON field grammar: numbers stay numbers, booleans stay booleans, and strings
must be JSON quoted (for example `--key 'repo="acme/api"'`). `--data @event.json`
reads a JSON payload from a file. It does not upload an artifact; a declared
File or Workspace field takes an existing reference, such as one returned by
another run. `ava run` retains the ordinary `--file FIELD=PATH` and
`--workspace FIELD=DIR` input facilities. Captured input belongs to the original
run. All state entries of an instance share one workspace folder, kept across operator
restarts, so a later state entry sees earlier edits, as flow steps do. Repeating an
existing key validates the key first, then returns the existing run without
replacing its input or claiming new files.

Definition and run lists display a TYPE column (`flow` or `machine`); detail
prints Kind. Machine definition detail includes states and key fields from
discovery. Add `--json` for machine-readable output: definitions carry `kind`,
run lists carry it on each run, and run snapshots carry `summary.kind`. Run
kind comes from persisted run metadata, even when the latest definition has
changed or the retained evidence has expired. History is paged; continue with
the returned `next_cursor` using `--cursor`.

A send returns `receipt_id`, `key` (the delivery's idempotency key),
`accepted_at`, `status` (`accepted` or `ignored`), and `duplicate`. Retrying the
same event/body/key returns the same receipt with `duplicate=true`, even after
the run finishes. Reusing the key with different JSON is a conflict. Admission
is not consumption: deferred, consumed and dropped events are later facts.

`after()` uses an absolute UTC deadline from state entry. It never interrupts
unfinished state entry work. The shared reducer chooses final completion first, then
the earliest expired timer, a returned internal event, and the first consumable
external event. A state transition cancels its remaining timers. `ava dev`
additionally enables the run-owned `timers:fire-next` control; the normal
operator refuses it.

The database retains machine runs, state entry snapshots, receipts, inbox and timers.
Restarting while waiting re-arms the original deadlines; overdue timers fire
once through the transition transaction, in deadline order. Restarting in the
middle of state entry work stops its old process and marks that state entry `interrupted`
with an unknown outcome. It does **not** repeat side effects automatically:

```sh
ava run retry RUN_ID --entry  # explicit new entry, same run/key/state
ava run cancel RUN_ID         # also works while parked on a timer/event
```

Check an uncertain external effect before retrying. General machine reruns are
not supported; a distinct business instance requires a different input key.
Ordinary unfinished flow runs still fail on restart rather than resume (P10).
Finished decision cards and receipts remain readable after restart, including
cards from earlier machine state entries. Stored records have no automatic expiry;
normal state pruning/deletion removes retained data but keeps the key-to-run
link, and history reads explicitly report `expired`. Removing the database
itself is a destructive identity reset.

Each state entry imports the latest source code on disk and records the operator's
catalog revision. Only a successfully loaded definition without the run's current
state produces `needs_attention` with `state_missing`. Restore that state and run
`ava run retry RUN_ID --entry` to resume without redoing its completed work or
changing the run/key. If the state is intentionally retired, cancel the run and
start with a new key instead.

A missing/renamed file or symbol, import error, or build error is different:
waiting runs stay `waiting`, with reason `definition_unavailable` and an error
summary in the event facts. No transition executes against a last-valid catalog
while the definition is unavailable. Original timers remain armed; source loading
is retried on sends (including receipt replays), timer delivery, explicit retry,
and operator restart, even when source watching is off. Only the affected
machine's unavailable definition triggers loading; unrelated broken files do not
cause reloads on a healthy machine's path. The owner coalesces loading to one
catalog refresh per loop pass and a shared minimum interval of one second.
Commands share a refresh completed after they arrived; a newer repair command
waits in a timed queue until the next allowed refresh, without sleeping on the
owner thread or blocking unrelated commands. A successful read is usable during
backoff, and closing a run or removing its catalog entry drops stale retry state.
Without watching, timer retries share exponential backoff per machine definition
(not per keyed run): one second, doubling to a 60-second cap. Send, explicit retry
and restart bypass that timer backoff, subject only to the shared minimum interval.
With watching, catalog reload wakes waiting runs directly, including those without
timers; unavailable timers do not poll discovery. Restoring the source clears the
reason and resumes the same run/key; overdue timers keep their original deadlines,
retain reducer priority and fire once in deadline order.

Sends during definition loss durably queue their JSON and return an `accepted`
ingress receipt before any processing. The restored definition validates queued
payloads before consumption. Invalid payloads produce a visible `rejected` fact
with the validation error; events not accepted/deferred by the restored state
produce an `ignored` fact. Receipts remain replayable, and valid/deferred events
follow the ordinary reducer/inbox rules. Admission still does not promise a
transition. If source loading fails during validation, the event stays queued.
Source-lookup races also leave the event queued with `definition_unavailable`,
without stopping the operator. Recovery uses one heap
wake per run, backing off from one second to 60 seconds even without a user timer
or while watching. Original timer deadlines are unchanged. If source becomes
unavailable again, source backoff or the watcher also gates that recovery wake.
A validation subprocess that exits nonzero, dies by signal or times out (60 s)
has a separate durable budget: three failures per receipt, with the same
backoff, then no automatic retries, so a hung validator holds the host for at
most three timeouts. The run's reason is then `validation_retry_limit` (run
detail in REST, gRPC `GetRunSnapshot` and `ava run show`), and its facts name the
receipt and the failure. The receipt and inbox row remain; restart does not reset
the failure count. The inbox is consumed in receipt order, so events queued behind
a capped event wait for it. After repairing the source, `ava run retry RUN_ID
--entry` resets the budget and validates the queued event again; it does not
start a new state entry on a waiting run.
A state entry that already started but fails to prepare remains `interrupted` and
requires explicit retry; restoring a definition never replays uncertain work.
A retry while source is still unavailable creates no new state entry and now fails
with `definition_unavailable`; a loaded graph without the current state fails
with `state_missing`. This corrects the former successful no-op: CLI exits
unsuccessfully, REST returns 409 and gRPC returns FAILED_PRECONDITION, each with
the reason. An interrupted run retains its unknown-outcome protection until a
successful explicit retry. A send queued on such a run while its state is absent
records `state_missing`, not `definition_unavailable`.

Editing a timer delay does not move an already-entered state's deadline; the
new delay applies on the next entry. Local state entries are therefore different from
Delta, where an instance remains pinned to its starting deployment. Application
environment comes from the operator; see [self-hosting environment files](
../README.md#application-environment-for-self-hosters).

### Operator UI

A machine run is a run: `ava operator` opens it on the normal run page. The
runs panel (titled Runs) lists it by its key label. The bottom pane has two
tabs, Logs and Timeline. The Timeline lists, in time order: each state entry entered
and left (with the arc it took), the state entry's steps, events received (accepted,
deferred, ignored, dropped, delivered) with their send receipts, timers armed,
fired or cancelled, waits, a cancel and the outcome. What the run waits for now
(its timer, deferred events, open waits) stays pinned on top, and Send event
stays in the run controls.

Hover an entry to light up its state and arc on the graph; click a state on
the graph to show only its entries. A state-entry row's Steps action opens that
state entry's steps; Logs shows its step logs, which the Logs tab also lists for the
whole run, labelled by state entry. Flow runs have the same tab with their steps,
waits, retries and outcome.

Selecting a state entry's step shows its typed output in the step panel's Run I/O
tab, read from the result route, as for a flow step. Under the run list, the
selected run appears between its parents and its children (from `ava.call` and
`ava.start_run`), with status and time ago; flow and machine runs look the same.
A call or start node on the graph links to its child run.

### Run-owned transport

The browser REST prefix is `/api/v1`. Start/list/show/cancel use the normal run
routes, not a separate machine start or list product. A run snapshot includes
`machine_json`; discovery includes `machine_spec_json`.

- `POST /runs/{run}/machine/events/{name}`: JSON body and `Idempotency-Key`,
  returns the receipt with HTTP 202.
- `GET /runs/{run}/machine`: current state/state entry, accepts, absolute `due`,
  pending events and the most recent 100 receipts.
- `GET /runs/{run}/machine/history?limit=20&cursor=...`: bounded transitions.
- `GET /runs/{run}/machine/entries/{entry}`: scoped snapshot, qualified nodes,
  entry/exit clocks, catalog revision and transition.
- `GET /runs/{run}/machine/entries/{entry}/events?limit=20&cursor=...`: bounded
  event facts. An empty page can still have a continuation.
- `POST /runs/{run}/machine/entries:retry`: explicit interrupted-state-entry repair.
- `POST /runs/{run}/machine/timers:fire-next`: dev-only timer control.
- `GET /runs/{run}/result?entry=&node_id=`: typed run, state entry or step result.
- `GET /runs/{run}/links`: native parent and child links.

The gRPC `ControlMachine` method carries these typed operations as JSON, next
to ordinary `StartRun`, `CancelRun`, `GetRunSnapshot` and `ControlWait`. These
are local APIs, not an authentication boundary: keep the operator on loopback
or behind an authenticated trusted proxy.
