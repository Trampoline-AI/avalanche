# Machine host retries

Machine operations no longer include a built-in hosted HTTP transport. This is
a breaking change from 0.20.x: start an instance through the normal run-start
path, and supply `host=` for sends outside workflows instead of API
scope/credential kwargs or environment-based target selection. The removed
`avalanche.machine_remote` and `avalanche.http_retry` modules have no deprecated
aliases. Hosts own network transport, authentication, connection lifetime and
retry budgets; the SDK imposes no HTTP retry policy on them.

Inside workflows, `machine.send` declares a graph node. The declared
`machine.start` node is retained pending the `ava-router-start` decision; there
is no explicit-host `Machine.start`. A machine-capable scheduler performs the
nodes; local operator integration is separate work. A declared send without an
explicit idempotency key uses the node's stable step key; a host must preserve it
across retries. Outside workflows, sends require an explicit idempotency key. Retry
an uncertain send with the same machine key, event, payload and idempotency key;
never generate a new identity merely because a response was lost. Start is
idempotent per machine key and returns `MachineStart`, including `created`.

These operation retries are separate from state-work step retries. The
in-memory `.test()` host exercises instance identity and receipt replay, but is
not a network retry or persistence proof. See [state machines](state-machines.md)
for the declared-step and explicit-host APIs.
