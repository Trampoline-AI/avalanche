# Delta compatibility protocol

`avalanche.PROTOCOL_VERSION` (`major.minor`) is one version for everything Delta
relies on from Avalanche. Delta records it at deploy; a worker runs a deployment
iff the major matches and the worker's minor is not older
(`avalanche.protocol.supports`). Baseline: **1.0 at Avalanche 0.15.x**.

- **Minor:** additive. Every deployment built on an older minor of the same
  major still runs.
- **Major:** an already-built deployment can no longer run. A Pydantic major
  bump is always a protocol major.
- **No bump:** UI/TUI-only changes, and internal changes Delta cannot observe.
  Package versions are independent: a package bump never implies a protocol bump.

## Covered

The worker-side surface Delta uses, private names included: the `avalanche.dag`
binding hooks called by Delta's flow executor, `resolve_step_signature`,
`BaseContext`/`BaseInput`, reference rebinding, `File`/`Workspace` JSON shapes,
agent steps and evidence, operator discovery and its cache, `LocalExecutor`, and
the operator proto.

PredictRLM has no separate number. Delta reaches it only through Avalanche
(`avalanche.agent.DirectPythonBackend`, agent evidence, `Workspace`/`File`), and
Avalanche keeps its `predict-rlm` range to one minor. A PredictRLM change Delta
would notice needs an Avalanche protocol bump.

## Release rule

A major change needs a `BREAKING-PROTOCOL` line in `CHANGELOG.md` explaining the
break. The release workflow checks this against the previous tag.
