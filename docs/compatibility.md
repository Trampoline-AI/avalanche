# Delta compatibility protocol

## Contract

`avalanche.PROTOCOL_VERSION` is one `major.minor` version for everything Delta
relies on from Avalanche, including private hooks and PredictRLM-backed behavior.
The baseline is **1.0 at Avalanche 0.15.x**. Avalanche stays installed in the worker.
`ProtocolVersion` is frozen and typed; `str(version)` returns `M.m`, and
`ProtocolVersion.parse("1.0")` parses that form.

Delta's admission rule is implemented by
`avalanche.protocol.supports(required, provided=PROTOCOL_VERSION)`:

```python
provided.major == required.major and provided.minor >= required.minor
```

- **Minor:** additive changes; older deployments in the same major still run.
  Keep old readers, call signatures and behavior within the major. Deprecation
  does not authorize their removal within that major.
- **Major:** an older deployment can no longer run, such as after removing a
  consumed hook or old JSON reader. A **Pydantic major bump forces a protocol
  major bump**. Reset minor to 0 when advancing major.
- Package semver is independent: pre-1.0 package minors may break author-facing
  APIs, but package bumps never imply protocol bumps. UI/TUI-only changes outside
  the covered worker surface require no protocol bump.

Delta reaches PredictRLM **only through Avalanche**:
`avalanche.agent.DirectPythonBackend`, agent evidence, and Avalanche's
`Workspace`/`File` interfaces. PredictRLM exports no separate protocol number.
Avalanche's `predict-rlm` dependency range stays bounded to **one minor**, currently
`>=0.13.0,<0.14`, and workers receive dependency updates through Avalanche releases.
Any PredictRLM change Delta would notice requires an **Avalanche protocol bump**.
`DirectPythonBackend` is re-exported lazily; `import avalanche` does not import
`predict_rlm`.

## Covered surface

Inventory from Delta main `d9e08ba0`, checked against Avalanche main `0178130`
(0.15.0) and its locked PredictRLM dependency; this PR adds the backend re-export.
The contract covers signatures, behavior, error semantics and serialized shapes:

- **DAG:** `avalanche.dag` hooks `_build_input_value`,
  `_build_node_binding_plan`, `_safe_issubclass`, `_inspect_runtime_params`,
  `_resolve_to_ref`, `_resolve_input_refs`, `_collect_implicit_parent_results`,
  `_reverse_graph`, `_bind_implicit_parent_results`, `_adapt_positional_call`;
  `avalanche.step_interface.resolve_step_signature`;
  `avalanche.runtime.BaseContext` / `BaseInput`;
  `avalanche.references.bind_references`; `avalanche.rebind_arguments`;
  `ava.step(slug=..., num_returns=...)`, `ava.source`, `ava.workflow`,
  `ava.Webhook`, `ava.RunContext` (`ava` means `import avalanche as ava`).
- **Files and agents:** `avalanche.file.WorkspaceFileRef`, `avalanche.file.File`
  and `avalanche.workspace.Workspace` JSON shapes (`ava.File` / `ava.Workspace`);
  `avalanche.workspace.snapshot_workspaces`;
  `avalanche.agent.capture_agent_evidence`; the agent specification
  `fn.__agent_step__`, `.runtime_kwargs`, `.with_workflow_defaults`;
  `avalanche.agent.DirectPythonBackend`. `Workspace` is not in `avalanche.file`.
- **Operator and executor:** `runtime.operator.registry.WorkflowRegistry`;
  `runtime.operator.discovery` (`WorkflowDiscoveryError`,
  `WorkflowDiscoveryTimeoutError`, workflow descriptors including `input_schema_json`);
  `runtime.operator.discovery_cache.DiscoveryCache`;
  `runtime.operator.workflow_metadata.node_docstring_lines_for_workflow` and
  `node_source_code_for_workflow`; `runtime.executor.LocalExecutor`; the operator
  proto (`src/runtime/operator/proto/operator.proto`), wire messages and generated
  Python interfaces.
- **Avalanche's PredictRLM implementation dependency:** `predict_rlm.PredictRLM`,
  `predict_rlm.Workspace`, `predict_rlm.DirectPythonBackend`,
  `predict_rlm.backends.jspi.JspiBackend`, and the agent evidence format. These
  are covered through Avalanche, not direct Delta imports or another protocol.
- **Pydantic:** its major version (currently 2), validation and JSON behavior.

Changing anything listed in a way an already-built deployment would notice
requires a protocol bump. Private names are not exempt; unobservable internal
changes do not require a bump.

## Release rule

Review covered changes and dependencies, choose the required bump, and update
`src/avalanche/protocol.py`. A major change requires a line containing
**`BREAKING-PROTOCOL`** in `CHANGELOG.md` under `## Unreleased` or the release
section matching the current `pyproject.toml` version; explain the break and migration.

Run `make lint`, focused tests, and
`uv run python scripts/check_protocol_changelog.py --base origin/main`.
PR CI compares against `origin/${{ github.base_ref }}`; release validation compares
against the previous tag from `git describe --tags --abbrev=0 "${GITHUB_SHA}^"`.
The stdlib checker parses source without importing Avalanche; a base without the
protocol file is the initial baseline and passes. Review still owns detecting
unversioned behavioral breaks and retaining old readers within a major.
