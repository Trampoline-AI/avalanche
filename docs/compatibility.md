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

## Protocol 2.1: runtime maps

`PROTOCOL_VERSION` is **2.1**. Record `workflow.required_protocol` as a deployment's
requirement: it is 2.1 for map declarations and 2.0 for non-map workflows.
A worker providing 2.0 cannot run a deployment requiring 2.1 under `supports`.
Package versions are unchanged by this addition.

The additive operator fields are `WorkflowTopologyV2.map_specs` (`MapSpecV2` with
mapped/broadcast argument names, effective maximum, inheritance and `fail_fast`),
`RunSnapshotV2.expansions`, and `NodeSnapshotV2.map_parent` / `map_index`.
`RunStatusEnvelopeV2.nodes_expanded` publishes run-scoped topology and actual
expected counts before any new child event. A chained group's not-yet-created
items have no node records. Traces and evaluations retain their existing schema.
Older clients may ignore the additive fields, but need the expansion event to
render live map children. Delta's map scheduler integration is not included here.

## Avalanche 0.18.0

Ray execution is removed: the `ray` extra, `RayExecutor` export, `--ray`
operator/dev flags, and the operator's `executor_factory`, `executor_backend`,
`ray_runtime_env`, and `ray_init_kwargs` arguments no longer exist. Use the
unchanged `LocalExecutor`; it is now always the default in-repository executor.
Delta's separate remote runtime is unchanged. **No protocol bump:** Delta does
not use the removed backend or its configuration.

## Release rule

A major change needs a `BREAKING-PROTOCOL` line in `CHANGELOG.md` explaining the
break. The release workflow checks this against the previous tag.
