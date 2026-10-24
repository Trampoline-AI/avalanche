# Delta compatibility protocol

## One version for the worker contract

`avalanche.PROTOCOL_VERSION` is the single `major.minor` version of **everything
Delta relies on from Avalanche**, including the private hooks and PredictRLM
surfaces listed below. The baseline is **1.0 at Avalanche 0.15.x**. Avalanche stays
installed in the worker; this contract does not replace it with deployment-vendored
code. PredictRLM is covered by Avalanche's protocol, not a separate protocol version.

`ProtocolVersion` is a frozen, ordered typed value with `major` and `minor` integer
fields. `str(version)` produces `M.m`; `ProtocolVersion.parse("1.0")` parses that
form. The helper `avalanche.protocol.supports(required, provided=PROTOCOL_VERSION)`
implements the admission rule Delta will use:

```python
provided.major == required.major and provided.minor >= required.minor
```

A worker on 1.2 can run a deployment requiring 1.0 or 1.2, but not 1.3 or 2.0.
A worker on 2.0 does not admit a deployment requiring 1.0. Ordering version values
is convenient for inspection, but is **not** a substitute for this admission rule.
This release exports the version and helper; Delta owns admission enforcement.

Package semantic versions and protocol versions are independent. A pre-1.0
Avalanche package minor may break author-facing APIs without breaking existing
built deployments. Package bumps never imply protocol bumps. UI/TUI-only changes
are outside this protocol, provided they do not change a covered worker surface.

## Covered surface

This inventory records what Delta consumes as of Delta main `d9e08ba0`, verified
against Avalanche main `0178130` (0.15.0) and its bounded PredictRLM dependency.
The contract includes signatures, behavior, serialized shapes and error semantics,
not merely the existence of the following names:

- **DAG construction and execution:** `avalanche.dag` private hooks
  `_build_input_value`, `_build_node_binding_plan`, `_safe_issubclass`,
  `_inspect_runtime_params`, `_resolve_to_ref`, `_resolve_input_refs`,
  `_collect_implicit_parent_results`, `_reverse_graph`,
  `_bind_implicit_parent_results`, `_adapt_positional_call`;
  `avalanche.step_interface.resolve_step_signature`;
  `avalanche.runtime.BaseContext` and `BaseInput`;
  `avalanche.references.bind_references` and `avalanche.rebind_arguments`;
  `ava.step(slug=..., num_returns=...)`, `ava.source`, `ava.workflow`,
  `ava.Webhook` and `ava.RunContext` (`ava` means `import avalanche as ava`).
- **Files, workspaces and agent steps:** `avalanche.file.WorkspaceFileRef`,
  `avalanche.file.File` and `avalanche.workspace.Workspace` JSON shapes (also
  exposed as `ava.File` and `ava.Workspace`);
  `avalanche.workspace.snapshot_workspaces`;
  `avalanche.agent.capture_agent_evidence` and the agent step specification at
  `fn.__agent_step__`, including `.runtime_kwargs` and `.with_workflow_defaults`.
  Workspace is defined in `avalanche.workspace`, not `avalanche.file`.
- **Operator and executor:** `runtime.operator.registry.WorkflowRegistry`;
  `runtime.operator.discovery`, including `WorkflowDiscoveryError`,
  `WorkflowDiscoveryTimeoutError` and workflow descriptors with
  `input_schema_json`; `runtime.operator.discovery_cache.DiscoveryCache`;
  `runtime.operator.workflow_metadata.node_docstring_lines_for_workflow` and
  `node_source_code_for_workflow`; `runtime.executor.LocalExecutor`; the operator
  protobuf service and messages in `src/runtime/operator/proto/operator.proto`,
  including their generated Python interfaces and wire representations.
- **PredictRLM through Avalanche:** `predict_rlm.PredictRLM`,
  `predict_rlm.Workspace`, `predict_rlm.DirectPythonBackend`,
  `predict_rlm.backends.jspi.JspiBackend` and the agent evidence format. The
  `predict-rlm` dependency range must stay bounded to **one minor**, currently
  `>=0.13.0,<0.14`, so PredictRLM changes reach workers only through an Avalanche
  release. Review the resolved dependency and the covered behavior on each release;
  the bounded range does not itself guarantee that upstream patch releases are
  compatible. Update and verify the worker lock/image through the Avalanche
  release process, rather than independently upgrading PredictRLM in workers.
- **Validation:** the Pydantic major version, currently 2 (`>=2,<3`), including
  validation and JSON serialization behavior used by these surfaces.

Changing anything listed in a way an already-built deployment would notice
requires a protocol bump. A private name is not exempt. Pure implementation
changes with no observable effect on this contract do not require a bump.

## Choosing a bump and retaining old readers

- **Minor:** additive behavior or shapes that preserve execution of older
  deployments in the same major. Examples include an optional field with a
  compatible default or a new hook alongside the existing one. Keep readers for
  old serialized forms throughout the major, and keep old call signatures and
  semantics usable. Deprecate and document migration first; deprecation alone
  does not authorize removal within the major.
- **Major:** an older deployment can no longer run. Examples include removing or
  changing a consumed hook/signature, requiring a previously absent JSON field,
  dropping an old reader, incompatible operator messages or evidence formats,
  or a breaking change in the covered PredictRLM behavior. A **Pydantic major
  bump always forces a protocol major bump**. Reset the protocol minor to 0 when
  advancing the major.
- **No bump:** an unobservable internal fix or UI/TUI-only change outside the
  covered surface. Do not derive the decision from the package version.

## Release procedure

1. Review this inventory against the implementation and Delta's consumed surfaces.
   Classify observable changes, including dependency and serialization changes, as
   additive or incompatible. Keep the PredictRLM range bounded to one minor.
2. Update `PROTOCOL_VERSION` in `src/avalanche/protocol.py` when needed. Keep old
   readers and execution behavior within a major; add migration guidance before
   removing them in a later major. Verify older deployments against the candidate.
3. For a major change, add a line containing **`BREAKING-PROTOCOL`** to the release's
   section in `CHANGELOG.md` (matching the current `pyproject.toml` version) or
   `## Unreleased`. Explain what breaks and how to migrate. For example:
   `- BREAKING-PROTOCOL: protocol 2.0 removes the old workspace reader; rebuild deployments.`
4. Run `make lint`, focused tests and
   `uv run python scripts/check_protocol_changelog.py --base origin/main`.
   For release validation, compare against the previous tag:
   `git describe --tags --abbrev=0 "${GITHUB_SHA}^"`. Build/check the worker's
   resolved dependencies and release artifacts through the normal release flow.
5. CI runs this check on pull requests against `origin/${{ github.base_ref }}`;
   the release validate job runs it against the previous tag, before build/publish.
   The script parses source with `ast` and the package version with `tomllib`,
   without importing Avalanche. A base without `protocol.py` is the initial
   baseline and passes. Invalid refs or malformed declarations fail closed.

The changelog gate catches an acknowledged version-major change, not an
unversioned behavioral break. Reviewers still own classification, old-reader
coverage and ensuring a dependency change has the appropriate protocol bump.
