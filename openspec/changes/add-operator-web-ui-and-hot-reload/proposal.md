## Why

The operator can rescan workflow source, but it does not expose catalog changes as live UI state and a run retains only node state, not the workflow topology it executed. A browser UI needs a current workflow view that reacts to reloads and a historically accurate run view that remains renderable after the workflow changes.

## What Changes

- Add an operator web UI with separate current-workflow and historical-run views, live state updates, and the operator control/observability surface already available to the TUI.
- Make workflow catalog reloads observable to clients so workflow creation, removal, metadata changes, and topology changes update the UI in real time.
- Persist an immutable workflow-topology snapshot with every run at run creation, including the topology and display metadata needed to render that run independently from the current catalog.
- Preserve existing runs across workflow reloads; reloads affect catalog/current-workflow views and future runs, never rewrite a run's recorded topology.
- Preserve bounded agent invocation inputs and outputs as structured evidence, including typed PredictRLM file values whose host paths receive file-specific presentation without copying or storing the files.
- Define browser-facing transport and asset-serving behavior while retaining the operator's local-first, loopback-default security posture.
- Keep large-run browser transport, live projections, inspector hydration, and rendering bounded so the web UI remains responsive under long agent traces and log histories.
- Add one canonical, bounded, vertically resizable run-log dock that shows retained and live logs across every step in authoritative sequence, scopes to a selected node on demand, and provides explicit auto-scroll without duplicating log state in the node inspector.
- Make operator and hot-reload lifecycle activity visible in the terminal through a configurable CLI log level and explicit reload outcome logs.

## Capabilities

### New Capabilities
- `operator-web-ui`: Browser interface for observing and controlling a local operator, with distinct current-workflow and historical-run views.
- `operator-workflow-reload`: Atomically refresh the workflow catalog and publish client-visible catalog changes.
- `versioned-run-topology`: Retain and serve an immutable topology snapshot for each run so historical runs remain accurately renderable.

### Modified Capabilities
- None.

## Impact

- `src/avalanche/agent/`: projection of declared PredictRLM file paths into existing agent evidence.
- `src/runtime/operator/`: workflow discovery/watch behavior, immutable catalog publication, run persistence models, update stream, gRPC protocol, and server hosting.
- `src/tui/`: shared domain models or protocol behavior may change; the Textual UI remains a separate presentation layer.
- `src/ava_cli/`: commands/options for launching or opening the web UI.
- New browser client source, frontend build/package integration, and tests.
- `pyproject.toml`, lockfile, packaging, and docs may need updates for the chosen browser transport and static assets.
