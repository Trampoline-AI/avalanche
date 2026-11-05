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

## Avalanche 0.18.0

Ray execution is removed: the `ray` extra, `RayExecutor` export, `--ray`
operator/dev flags, and the operator's `executor_factory`, `executor_backend`,
`ray_runtime_env`, and `ray_init_kwargs` arguments no longer exist. Use the
unchanged `LocalExecutor`; it is now always the default in-repository executor.
Delta's separate remote runtime is unchanged. **No protocol bump:** Delta does
not use the removed backend or its configuration.

## Retries-and-waits: retries and graph waits

Protocol **3.0** replaces the previous 2.0 worker contract: discovery carries
per-invocation retry/uncertainty policies, worker exceptions carry terminal and
unknown-outcome flags, and `RunContext.step_key` uses the deployed invocation
identity. This requires redeployment, not a compatibility adapter. Delta's
scheduler enforcement and deployed waits require the run-manifest scheduler
follow-up; retries-and-waits lands after run-manifest. Local Avalanche supports
the full local contract. No release is made by this preparation.

## Release rule

A major change needs a `BREAKING-PROTOCOL` line in `CHANGELOG.md` explaining the
break. The release workflow checks this against the previous tag.
