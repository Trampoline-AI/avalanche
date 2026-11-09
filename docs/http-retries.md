# Machine host retries

Machine operations no longer include a built-in hosted HTTP transport. This is
a breaking change from 0.20.x: start an instance through the normal run-start
path, and supply `host=` for sends outside workflows instead of API
scope/credential kwargs or environment-based target selection. The removed
`avalanche.machine_remote` and `avalanche.http_retry` modules have no deprecated
aliases. Hosts own network transport, authentication, connection lifetime and
retry budgets; the SDK imposes no HTTP retry policy on them.

Inside workflows, `ava.start_run(target, input, when=...)` declares one generic
start for flows or machines, and `machine.send(..., when=...)` declares a send.
There is no `Machine.start`, `deliver` or public host-start API. A scheduler
performs the nodes; local operator integration is separate work. A declared send
without an explicit idempotency key uses the node's stable visit-qualified step
key; a host must preserve it across retries. Outside workflows, sends require an
explicit idempotency key. Retry an uncertain send with the same machine key or
run ID, event, logical payload and idempotency key; never generate a new identity
merely because a response was lost. A keyed start returns `RunStart`, including
`created` and `read_state`; an existing key ignores replacement input/files and
may resolve to an `expired` run. Native File/Workspace admission and retention
belong to the host, before an accepted event receipt.

These operation retries are separate from state-work step retries. The
in-memory `.test()` host exercises instance identity and receipt replay, but is
not a network retry or persistence proof. See [state machines](state-machines.md)
for the declared-step and explicit-host APIs.
