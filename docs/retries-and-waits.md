# Retries, uncertain outcomes and graph waits

## Step safety

```python
import avalanche as ava

@ava.step(retries=3, on_uncertain="hold")
async def charge(order: Order, run: ava.RunContext) -> Receipt:
    if order.amount <= 0:
        raise ava.NonRetryableError("invalid amount")
    try:
        return await payments.charge(order, idempotency_key=run.step_key)
    except PaymentReadTimeout as exc:
        raise ava.OutcomeUnknownError("provider may have accepted the charge") from exc
```

`@ava.source`, `@ava.step`, `@ava.agent_step`, `@ava.classifier_step` and
`@ava.dest` share the same `retries` and `on_uncertain` contract (including
inline `ava.agent.step`).

`retries` counts extra attempts after the first. The default budget is seven
extra attempts, bounded by generation 8; a larger declaration is capped, not an
unbounded crash-retry loop. `node.policy.effective_retries` exposes the bound.
Known retryable errors and uncertain-outcome retries share this budget.
`NonRetryableError` skips remaining retries (`retryable=false`). An ordinary
reported exception is retryable; use `OutcomeUnknownError` when its external
outcome is ambiguous.

`on_uncertain` applies to ambiguous outcomes, independently of known failures:

| Policy | On uncertainty |
| --- | --- |
| `"retry"` (default) | Retry only within the remaining budget; fail with outcome unknown when exhausted |
| `"hold"` | Immediately park for reconciliation, even if retries remain |
| `"fail"` | Fail with outcome unknown, without replay |

**`retries=0` never implies hold.** With the default uncertainty policy it means
no automatic replay and an outcome-unknown failure. A policy never proves an
external effect happened exactly once. In a durable host, hold/fail additionally
require a committed, acknowledged attempt-start fence before user code, stale
attempt fencing, and uncertainty classification for lost reports or failed
observation writes. The SDK declaration alone does not implement that control
plane. Embedded execution can classify explicitly raised `OutcomeUnknownError`;
it cannot recover an interrupted process.

`RunContext.step_key` is `run_id:invocation_id` (with `:index` for a mapped item): it stays the same across attempts
and differs across runs. It is available only inside an invocation. Send unchanged
action parameters and this key to a recipient that **enforces** deduplication;
check its retention window. Use a business-operation key for deduplication across
different runs. Multiple distinct effects inside one agent step need distinct
operation keys. No per-tool checkpointing or agent-internal approval is implied.

A hold is a built-in decision addressed by the original invocation ID. An operator
checks the recipient, then submits one of:

```json
{"kind": "done", "output": {"receipt_id": "confirmed"}}
{"kind": "retry"}
{"kind": "fail"}
```

`done` requires output validated against the step's return annotation. `retry`
authorizes **one** new attempt, preserving the key, without resetting the automatic
budget or exceeding generation 8. Require authoritative reconciliation or valid
recipient deduplication, including protection against an earlier request still
finishing. A missing lookup is not proof that no effect occurred. The submission
requires `run:decide`, an identity and a reason.

## Three graph-level waits

Waits are graph nodes composed with `>>` and `&`, never per-step `await` calls:

```python
from datetime import timedelta
from typing import Literal
from pydantic import BaseModel

class Approval(ava.Event):
    kind: Literal["approve", "reject", "edit"]
    note: str = ""

class Signed(BaseModel):
    envelope_id: str

@ava.workflow
def contract_flow():
    draft = write_contract()
    review = ava.decision(
        Approval, prompt="Approve this contract?", show=draft,
        timeout=timedelta(days=2), on_timeout=Approval(kind="reject"),
        approvers=["reviewer"], assignees=["contracts-team"], slug="review",
    )
    signed = ava.wait_for_event(
        Signed, key="docusign.signed", timeout=timedelta(days=14),
    )
    return (draft >> review >> send_for_signature(draft, review)
            >> signed >> ava.sleep(timedelta(days=1)) >> follow_up(signed))
```

- `ava.sleep(duration)` accepts seconds or `timedelta`, and returns `None`.
  `ava.sleep(until=timezone_aware_datetime)` uses an absolute wake time. Specify
  exactly one. The deadline is computed once when the node arms.
- `ava.wait_for_event(Model, key=..., timeout=...)` returns `Model | None`;
  `None` means timeout. `timeout` is required: a duration or explicit `None`.
- `ava.decision(Event, prompt=..., show=..., timeout=..., on_timeout=...)`
  returns the declared Event. `timeout` is required: a duration or explicit
  `None`. `on_timeout` must be an instance of the declared Event; when omitted,
  expiry fails with `decision_timeout`. A timeout is audited as a **system**
  resolution, not as a human rejection. Escalation can use an explicit typed
  timeout outcome that a downstream step interprets.

A decision pins its schema, prompt and shown artifact. `show` accepts a JSON
value, Pydantic model, or an upstream node reference (including a selected tuple
item). Audit identity, time, reason and reviewed schema/artifact are outside
author-controlled Event fields. `assignees` routes attention; it is not an ACL.
Optional `approvers` further restricts `run:decide`. Event permission cannot
resolve a decision. A second different decision conflicts; it cannot overwrite
the first or resurrect a withdrawn run.

Reviewed JSON up to 4 KiB is displayed inline. Larger values are pinned as a
SHA-256 digest of canonical JSON (sorted keys, compact separators, ASCII escaping),
with the byte length and a partial 1 KiB preview marked `truncated`. Cards and
acceptance receipts share that same descriptor; it is not a complete artifact
viewer or a durable blob store. Review the full artifact separately before approving.

## Events and acceptance receipts

Address events to **one run and one exact declared key**:
`POST /v1/runs/{run_id}/events/{key}`. Keys may contain punctuation; encode each
path segment. There is one schema and one logical consumer per key. Duplicate
consumer keys fail graph construction. Unknown keys are rejected; they are not
accepted as unvalidated blobs. No content routing, broadcast or cross-run webhook
correlation is provided.

Send a JSON model payload with `Idempotency-Key`. The schema is validated before
acceptance, even before the wait arms. Early events are buffered from admission;
the first accepted event resolves the one-shot consumer, in accepted order. Each
event is consumed at most once. Retained receipts outlive payload consumption:

- Same `(run, key, idempotency key)` and payload returns the original receipt.
- Reusing it with a different payload conflicts (409).
- Invalid payloads are rejected (422), unauthorized senders (403), unknown keys
  (404), and full inboxes or closed runs (409, `inbox_full` / `run_closed`).

Defaults are 64 unconsumed events and 1 MiB queued payload/run. Local queues are
in memory. Durable hosts retain unconsumed payloads until run end, use blobs over
4 KiB, preserve dedupe receipts for run-history retention, and enforce configured
limits **before** acceptance. An accepted receipt does not promise consumption
if the run is later cancelled.

## Local development

`LocalExecutor` implements the same authoring contracts **without durable
restart**. The existing embedded driver and worker pool remain thread-backed.
Waits occupy no worker and create no per-wait thread: the driver parks on one
condition and wakes on a deadline, event, decision, cancellation or task completion.
Other ready branches keep executing, including with `max_workers=1`. Nullable
event timeouts pass through implicit `>>` composition. Execution-service receipts
flow across waits without opening service sessions for them; a wait-only run has
an empty receipt tuple.

```python
run = contract_flow().run(
    fast_timers=True,
    events={"docusign.signed": Signed(envelope_id="synthetic")},
    decisions={"review": Approval(kind="approve")},
)
result = run.result(timeout=5)  # or await run
```

`fast_timers` skips **sleep only**, not pending decisions or event timeouts.
Preloads go through the same validation and acceptance transitions. Test helpers:

```python
from avalanche.local_waits import LocalPrincipal

reviewer = LocalPrincipal(name="reviewer", permissions=frozenset({"run:decide"}))
run = contract_flow().run()
assert run.wait_until_waiting(timeout=5)
run.decide("review", {"kind": "approve"}, principal=reviewer,
           reason="checked pinned draft", idempotency_key="review-1")
run.advance_time(timedelta(days=15))  # controlled deadline advancement, not a virtual clock
run.cancel()  # withdraw pending waits; an accepted receipt remains replayable
```

`run.send_event(key, payload, idempotency_key=..., principal=...)` exercises early
buffering and permission refusals without an HTTP server. Embedded principal
objects are **trusted local/test inputs**, not authentication credentials.

With `ava dev examples/durable_waits.py`, use the running operator:

```sh
ava event send RUN_ID docusign.signed '{"envelope_id":"synthetic"}' --idempotency-key event-1
ava run decide RUN_ID decision_1 '{"kind":"approve"}' --reason 'checked draft' --idempotency-key review-1
```

`--connect HOST:PORT` selects the gRPC listener. The corresponding local REST API
is mounted at `/api`: `GET /v1/runs/{run}/waits` returns pinned schemas/cards;
`POST /v1/runs/{run}/decisions/{node}` accepts `{"outcome": {...}, "reason": "..."}`
with `Idempotency-Key`. CLI and UI use these same validators. Local operator
identity is the **operator OS account**, not an identity supplied in a body.
Loopback reachability is not authentication: do not expose this development
operator as a production authorization boundary. Durable hosts must authenticate
principals and enforce tenant/run permissions themselves. No automatic TTY prompt
runs in noninteractive mode.

Wait lists are paged: `GET /v1/runs/{run}/waits?offset=0&limit=64` returns
`waits_json` and an optional `next_offset`; follow that offset until absent.
`ControlWait` and the Python provider expose the same fields, and the browser
follows every page. The default and maximum page size are 64. Control frames stay
within 2 MiB: an oversized response is refused as `response_too_large` without
breaking subsequent decisions. Reduce the page size for oversized aggregate
metadata. Decision reasons are limited to 4,096 characters.

Before a coordinator exits normally, the operator retains an immutable final
wait snapshot with bounded display previews, including system timeout audits and
withdrawn cards. Terminal lists read that snapshot, accepted receipts still
replay, and new writes remain refused. These records last only as long as the
operator's in-memory run; a forced coordinator kill cannot export a final snapshot.

Run the synthetic example with `uv run python examples/durable_waits.py`.

## Per-item decisions and composition

```python
reviews = ava.decision.map(
    Approval, drafts, prompt="Publish this article?",
    timeout=timedelta(days=2), on_timeout=Approval(kind="reject"),
    approvers=["reviewer"], max_items=8,
)
```

`items` is a static list or a deferred node producing a list. By default each
item is the shown artifact. Optional `show=fn` receives one item and returns a
Pydantic model to show instead. The default bound is 8, with a hard cap of 64; overflow fails
before any item becomes addressable. Expansion publishes item identities and
schemas together. An item is addressed as `decision_1[0]` (or the declared local
slug plus `[0]`); its operation key is `run_id:invocation_id:index`. Each decision
has its own permission check, resolution and audit receipt. One submission cannot
resolve siblings. Rejection is typed data and does not cancel siblings. The group
returns outcomes in input order after all items resolve, including typed timeout
outcomes; an unhandled decision timeout fails the group/run.

Use a closure to include shared context without changing the input items:

```python
publication_policy = "Publication policy v1"

def show_review(draft: Draft) -> ReviewItem:
    return ReviewItem(draft=draft, policy=publication_policy)

# Inside a workflow. show is a function, not a template string.
reviews = ava.decision.map(
    Approval, drafts, prompt="Publish?", show=show_review, timeout=None,
)
```

`show` runs as an implicit pure step for each item, on the flow worker, never in
the operator/control plane. It shares the normal step retry contract (including
`NonRetryableError`) and bounded worker pool. Its stable step identity is
`decision_1.show[0]`; the associated decision remains `decision_1[0]`. Hosts journal
the step output through the same execution/hook boundary as other steps, then
replay that output rather than re-evaluating `show`. Embedded execution remains
process-local and does not add a persistent journal of its own. The console card
and acceptance receipt use that same output, subject to the existing bounded
artifact previews. The development operator graph keeps the declared map group
as one node and attaches step previews to it; individual cards keep their item
identities. A rendering error exhausts that item's normal retry budget,
reports a step/decision failure and fails the run; it is not a rejection outcome.
Overflow is checked before any `show` invocation or addressable child is created.
A decision's timeout starts only once its shown output is ready.

### Quorum: fan in over decision results

There is no quorum primitive. Define the business rule in a normal step:

```python
@ava.step
async def quorum(reviews: list[Approval]) -> bool:
    return sum(review.kind == "approve" for review in reviews) >= 2

@ava.workflow
def publication_vote():
    reviews = ava.decision.map(
        Approval, article_drafts(), prompt="Publish this article?", timeout=None,
    )
    return reviews >> quorum()
```

This fan-in observes the complete ordered result list; it does not implement an
early quorum, cancel unreviewed items, or authorize one item from another's vote.
Choose a typed `on_timeout` when the rule should count timed-out votes.

### Approve all: one decision gating the entire list

For one reviewer action covering the complete list, do not use an item map:

```python
@ava.step
async def publish_all(drafts: list[str], approval: Approval) -> list[str]:
    return drafts if approval.kind == "approve" else []

@ava.workflow
def approve_all():
    drafts = article_drafts()
    approval = ava.decision(
        Approval, prompt="Publish this complete list?", show=drafts, timeout=None,
    )
    return approval >> publish_all(drafts, approval)
```

This creates one pinned artifact/schema and one audit decision for the whole list,
not a bulk endpoint that silently resolves independent item cards. Both patterns
are exercised in `examples/durable_waits.py` by the smoke gate.

## Hosting/discovery contract

Discovery, cache schema 15 and `FlowInfoV2` carry `node_policies` and `node_waits`
by **invocation ID**, separate from event topics. The subprocess payload and
cache require these maps; old caches are invalidated and rebuilt, never assigned
invented policy values. `WaitSpec` pins JSON Schema, deadline specification,
artifact reference, prompt, timeout outcome and approver policy. A host dispatches
only executable nodes: wait nodes have generation 1 and no worker attempts.
The gRPC `ControlWait` request carries no reviewer identity field.

No package/protocol number, tag or release is changed by this unreleased work.
A combined release must coordinate the hosting runtime's enforcement before
claiming durable waiting, start fences, fair re-admission or crash recovery.
