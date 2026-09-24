# Changelog

## Unreleased

- Updated the locked AnyIO dependency to 4.15.1 to fix TLS hostname validation
  for internationalized domains and process-worker hangs caused by Python stderr writes.
- Updated Vitest and its mocker to 4.1.11 to fix development-server file disclosure;
  migrated test commands and stabilized asynchronous UI test completion for Vitest 4.

## 0.5.3

- Operator viewers automatically select newly arriving runs while viewing Current
  or the latest run, including after starting a run; older selections remain pinned.
- Operator header breadcrumbs stay centered when space permits, then consume
  remaining side space before truncating from the left without wrapping or
  overlapping the brand or connection status.
- Fixed operator UI builds on case-insensitive filesystems by separating the
  step-interface component filename from its schema module.

## 0.5.2

- Classifier calls now accept runtime `questions=` that replace optional decorator
  defaults. Each call validates and retains its own questions, including concurrent
  calls with different candidates or rubrics; missing or invalid questions fail
  before contacting TypeSafe.
- Classifier inspection distinguishes default questions from runtime-only steps
  and shows each invocation's actual rubric, or an explicit unresolved-question
  failure. Operator lifecycle validation preserves those rubrics through retention.

## 0.5.1

- Expanded the Avalanche authoring skill with classifier question-object examples
  for routing, overlapping labels, scoring, and evidence checks, plus guidance on
  structured rubrics, candidate selection, batching, and uncertainty.
- Starter-project documentation now uses `uvx avalanche-ai@latest init` so
  initialization requests the latest published release rather than a cached version.

## 0.5.0

- Classifier answers remain valid when TypeSafe omits either token-usage count or
  reports it as null; unknown counts remain distinct from zero.
- Rejected workflow-success events now release already-accepted result payloads,
  preventing classifier protocol errors from exhausting result-store quotas.
- Live classifier calls remain reachable after the 500-call descriptor window
  fills, without moving the currently visible page or prefetching collapsed details.
- Classifier history pagination preserves its original snapshot cursor as event
  retention advances, while still rejecting forged or expired continuations.
- DAG cards now align by their vertical centers, keeping same-row arrows straight
  when connected cards have different heights. Row spacing follows measured card
  heights so tall siblings remain separated when zooming between compact and
  detailed views.
- Source, ordinary, destination, and agent steps now share the classifier's
  **Step interface** panel, with annotated inputs, return types, and expandable
  nested schemas. Agent-call fields remain separate. Run views retain the
  executed interface after source changes and allow inspection of non-agent nodes.
- Source, ordinary, and destination DAG cards now show annotated inputs and return
  types at detailed zoom; agent cards keep their existing call-field display.
  Empty input and output columns keep their headings without placeholder text.
- Standard-step sidebars now separate **Definition** and **Code** tabs, opening on
  Definition and loading source only when Code is selected.
- Classifier definitions now show annotated step inputs and outputs separately
  from an optional `input_model=` state schema, including expandable nested fields.
  Declared state models validate calls before TypeSafe requests; historical calls
  retain their original schemas.
- Classifier sidebars now lead with questions, visually grouped with classifier
  input, and place step inputs and output together in a separate bottom panel.
- Classifier DAG cards now show annotated step inputs and return types in the
  same two-column layout as agent cards, hiding the fields at compact zoom.
- DAG card field sections now size to their contents, removing the empty row
  beneath single-input classifiers while preserving the shared bottom padding.
- Classifier steps now load the nearest `.env` at runtime when `TYPESAFE_API_KEY`
  is absent, preserving exported variables without loading credentials during discovery.
- Simplified the classifier example and quick-start questions to plain-text
  instructions and criteria, without nested description objects.
- Fixed classifier failures on TypeSafe's rounded probability distributions,
  allowing meeting follow-up classification to complete without altering the returned evidence.
- Added an operator-run meeting follow-up example combining agent extraction,
  per-item TypeSafe classification, and department-based routing to illustrative
  Linear, Attio, and Jira destinations, with source-quote validation, a separate extraction signature,
  and a bundled cross-functional transcript loaded automatically without run inputs.
- Meeting follow-up routing maps every department to one of three parallel destination
  nodes named Linear, Attio, and Jira, each returning a demo plan without service calls.
- Removed the meeting demo's live service clients, publishing configuration, and receipts;
  extraction, classification, and the seven-node workflow remain intact.
- Avalanche authoring guidance now requires operator-based execution without
  standalone runners and separate files for all non-inline signatures.
- Added bodyful `@ava.classifier_step` with runtime TypeSafe credentials, fixed
  Choice/Noul/Score questions, typed probability-preserving results, and
  workflow-scoped model/timeout defaults. Each call captures evidence separately
  from the node return, including answers retained when later postprocessing fails.
- Browser run views refresh prepared nodes and topology when a run leaves the
  requesting state. Python client run initialization preserves issued node
  activity bindings for classifier evidence.

### Operator web interface

- Agent graph nodes now have a violet badge with a robot icon that remains visible
  when zoomed out, matching the classifier badge treatment.
- Zoomed-out graph cards center the step title itself, with badges above and
  summaries below, keeping agent, classifier, and standard step titles aligned.
- Classifier inspectors use compact question rows with Avalanche typography,
  cyan classifier styling, ranked probabilities, confidence, and type badges.
  DAG node labels show per-type counts without a redundant question total.
  Expanded criteria are indented beneath question definitions with smaller labels,
  and the Definition panel has reduced outer padding. Call inputs appear in a
  read-only JSON editor with syntax highlighting, line numbers, and folding.
  Meeting follow-up questions and option descriptions are shorter.
- Classifier run results omit question and option descriptions, keeping names and
  probabilities; full descriptions remain in Definition.
- Choice expansion reveals remaining options in place instead of repeating results;
  rows with three or fewer options need no expansion. Missing Noul criterion
  descriptions stay blank, and boolean criteria are labeled True/False.
- Classifier run inspectors no longer render a redundant single Calls tab.
- Classifier call lists now receive call numbers and compact typed answers in
  paginated and live activity descriptors, without input previews or background
  detail prefetching. Full details load only for expanded calls and remain the
  only data subject to the eight-entry, 8 MiB detail cache; evicted details reload
  automatically on expansion without losing row summaries.
- Classifier calls now use a compact, full-width table instead of individual cards,
  with aligned answer, status, and duration columns, 25-call pages, and Previous/Next
  navigation. Calls start at index 1 in ascending order, all collapsed by default.
  Rows expand inline without fetching details for other rows or pages.

## 0.4.1

- Changed Avalanche's license to MIT to match predict-rlm's license. The Python
  distribution and operator UI ship the MIT license text and metadata; third-party
  license notices remain intact.

## 0.4.0

- Late browser run-start responses no longer override newer navigation, retained
  runs remain inspectable after their current workflow definition disappears, and
  pending cancellation disables repeat submissions.
- Open agent trace details take priority over background summary hydration within
  the bounded browser cache. Evicted details and failed requests expose explicit
  reload and retry controls instead of remaining stuck in a loading state.
- Live status updates no longer reset compact timeline scrolling. Mobile workflow
  drawers keep their last rows reachable, and bundled fonts preserve Markdown italics.
- The floating and expanded browser timelines now use a subtle, trackless scroll handle over
  full-width rows, clear of their text. It stays faintly visible when scrollable
  and reaches full opacity on hover.
- The expanded run browser now keeps the Timeline name. Both timeline sizes use
  dark 2px separators above Current, dark 1px separators below it, and lighter
  1px separators between runs.
- Local and embedded operator browser interfaces now share one workflow workspace.
  The timeline switches between Current and historical runs. Selecting the newest
  run follows new runs; selecting an older run pins it, and Current stops following.
  Selection intent survives reconnects, and Current keeps node definitions available.
- Added an all-runs browser beside the workflow graph, with 25-run pages, status
  and inclusive local-date filters, and run-ID search. The floating timeline expands
  into a full-height left-hand browser without replacing node inspection. Collapse
  and Escape restore the compact timeline; pagination covers loaded history only.

### Operator web interface

- The operator UI now bundles Delta Console's Nacelle Regular and SemiBold fonts,
  while keeping code and filename references monospace.
- The local workflow sidebar now follows Delta Console's padded, square-cornered
  rows and straight tree connectors reaching the target icon, retaining source
  filenames. Selected labels retain Delta's brand blue on hover. A sole scan
  target sits directly above its workflows; zero or multiple targets use a flat
  workflow list without inferring ownership. Removed the Navigator/Explorer headings.
- Skill popups now display declared package requirements and module names beneath
  their instructions, using the existing declaration metadata.
- Current-state tool entries expand into full Python source viewers instead of
  docstring-only descriptions; discovery caches refresh to include tool source.
- Agent traces now lead with each turn's reasoning and separate main/sub-model costs, while
  code, compact terminal output, tool calls, and model calls remain expandable.
- Agent run inspectors now surface status, duration, models, per-model costs, and
  iterations in the sidebar header, with Trace and Run I/O tabs below. Current
  definitions remain separate from historical run schemas and retained values.
- Run inspector headers no longer include the redundant **See current state** button;
  use the run timeline or workflow breadcrumb to return to the current workflow.
- The local browser header now identifies its sole configured workflow scan target,
  retains the local-operator label when several targets are configured, and links
  the workflow breadcrumb back to its current state.
- The Explorer now collapses to a persistent rail and previews over the workspace
  on pointer hover. Its compact footer control stays in place, and its non-wrapping
  label appears only after the width transition finishes.

## 0.3.2

- Agent steps can cross Ray/cloudpickle boundaries without serializing process-local
  workflow context variables. Omitted agent options retain their singleton identity
  after serialization instead of becoming invalid skills/tools overrides.
- Run-summary pagination now accepts forward live observations while preserving
  source continuity, exact continuation bindings, and bounded page traversal.
- `LocalExecutor(max_workers=1)` now uses true serial admission: it checks for
  cancellation before every node and does not start a ready sibling after a
  preceding node cancels or fails. Running local work remains cooperative.
- Constrained DataFramely to its supported 1.x API and Polars to the qualified
  1.34 series so fresh installations retain compatible typed table schemas.
- Avalanche and `@trampoline-ai/operator-ui` now share versions and a single Avalanche
  release tag. Both packages are built and checked before either publishes, and the
  GitHub Release waits for both registries. Failed publication jobs can reuse their
  validated artifacts without re-uploading existing packages; npm retries verify
  archive integrity before skipping an existing version.
- Consolidated Python, terminal, and browser tests around core execution, data
  integrity, recovery, and interaction scenarios; removed redundant test scaffolding
  and static-copy/schema inventories. `make test` now includes browser tests.
- Model-to-Arrow conversion JSON-serializes only JSON-backed fields, preserving
  arbitrary binary values in native fields and nested models.
- Injected `RunContext` parameters now share worker-resolved producer lineage under
  Ray instead of retaining a separately serialized, stale context.
- Lance tables preserve their qualified identity across worker serialization, so
  Ray stream consumers can use their matching upstream append results.
- Serialized initial Lance dataset creation to prevent competing first appends
  from overwriting committed rows; ordinary appends remain concurrent.
- Removed obsolete test-double and compatibility fallbacks; browser catalogs now
  retain the operator's revision, including zero, and TUI refreshes no longer
  silently swallow unexpected rendering errors.

## 0.3.0

### Dependencies

- Updated the operator web workspace's transitive `nanoid` package to 3.3.18.

### Operator CLI

- `ava operator` and `ava dev` now use `[tool.avalanche].flow_targets` when
  positional flow targets are omitted. Explicit targets replace that configured
  list, and commands without either source fail instead of scanning the current
  directory.
- Discovery failures now stop local operator and development services rather
  than leaving a stale workflow catalog running.
- `ava dev` starts the operator and browser UI under one lifecycle supervisor.
- `ava init` now configures its starter workspace to scan `src/`, so
  `uv run ava dev` discovers newly added starter workflows without a local wrapper.

### Workflow execution

- `LocalExecutor` now runs independent, dependency-ready workflow nodes
  concurrently in a bounded thread pool while preserving fan-in argument,
  receipt, hook, and node-log behavior.
- Concurrent `Table.append()` calls now preserve successful Iceberg and Lance
  appends; commit ordering is unspecified. Iceberg catalog conflicts retry with
  jitter for up to 30 seconds before surfacing `CommitFailedException`.
- The cursor example keeps per-model embedding work parallel and commits both
  results through one fan-in transaction.

## 0.2.0

### Continuous integration

- CI actions and browser tooling now run on Node.js 24.
- Release validation now rejects prerelease hotfix tags; only major and minor
  prereleases are accepted.

### Documentation

- Replaced the published Avalanche documentation source with a workflow-first Docsalot site covering onboarding, authoring, local operation, advanced data workflows, extensions, and complete public API and CLI reference.

### Operator web interface

- Source-code inspectors now end at the viewport edge on narrow screens, preserving the
  inset around the full-height code surface.
- Source and destination nodes now show their first docstring lines, matching standard
  step summaries.
- Extracted the embeddable operator UI into the `@trampoline-ai/operator-ui` package, with
  typed host and workflow-workspace APIs for local and hosted shells.
- Prepared `@trampoline-ai/operator-ui` for independent GitHub Packages releases with archive
  verification and a versioned release workflow.
- Added a package-specific README so GitHub Packages displays operator UI installation and
  embedding guidance.

## 0.1.5rc1

This prerelease carries the V2 server, converter, and generated bindings for compatible
operator clients.

### Operator transport

- Added an additive V2 project-summary cursor for consistent run-summary page
  chains without changing lifecycle watch or reset cursors.
- Added a typed terminal-seal activity to V2 run updates and snapshots so clients
  can replay, retain, and display the final run status and optional reason.
- Added a V2 catalog-reload notice so connected clients retain the current flow
  catalog until a deployment change's complete discovery reload succeeds.

## 0.1.4

### Operator transport

- Added `OperatorServiceV2`, an additive native gRPC contract with generated
  Python and TypeScript bindings.

## 0.1.3

### Operator discovery

- Source discovery no longer unloads dependencies imported from excluded
  directories such as a project-local `.venv`, avoiding duplicate native-module
  load failures while scanning workflows.
- `ava operator` and `ava dev` now accept `--discovery-timeout SECONDS` for one
  discovery scan. The positive, finite limit defaults to 60 seconds, up from 15.
- Discovery now logs a warning when its deadline expires and incomplete results
  are discarded.

## 0.1.2

### Dependencies

- Declare Protobuf as an Avalanche runtime dependency required by the operator's
  generated gRPC bindings.

## 0.1.1

### Workspace initialization

- `ava init` now rebuilds its virtual environment after moving a staged workspace
  into its final location, so generated `ava` entry points use the final path.

### Dependencies

- Avalanche now installs PredictRLM's `codex-lm` extra by default, enabling Codex
  LM support.

## 0.1.0

### Operator web interface

- Added an opt-in local React operator interface (`ava web`) with workflow
  discovery, live DAG replacement, immutable historical run canvases,
  launch/cancel controls, logs, and demand-loaded agent evidence.
- Added a browser listener and packaged browser assets. Loopback remains the
  default; non-loopback binding requires the explicit `--trusted-proxy`
  acknowledgement.
- Run topology now retains only versioned agent input/output field schemas,
  while bounded trace descriptors expose stable PredictRLM header, usage, and
  telemetry metadata without embedding declaration instructions or complete
  trace bodies in structural snapshots.
- Unchanged discovery results no longer advance catalog revisions, and the web UI
  retains workflow/run navigation on narrow viewports with accessible input and
  repeated-node labels plus WCAG AA secondary-text contrast.
- Workflow cards now show contained typed field lists while declarations remain in the inspector.
  Canvases retain depth-aware edge routing, live durations, stronger execution states, and `Run`
  labels.
- Agent steps now use an explicit DAG-card label and accent, including historical
  runs classified from their retained agent field schemas.
- Successful and failed nodes retain their neutral borders; only their titles
  and status labels use the corresponding outcome color.
- The current workflow canvas uses React Flow's neutral dotted blueprint field;
  historical run canvases retain their separate neutral presentation.
- Run logs now render bounded ANSI SGR color and text-style sequences without
  interpreting log content as HTML.
- Node-scoped run logs now retain canonical node IDs, including repeated-node
  suffixes, so selecting a log node preserves its filtered records.
- Starting a workflow now navigates directly to its retained run snapshot as
  soon as the operator publishes the run ID.
- Agent steps now default PredictRLM to quiet execution; workflows and
  individual steps can explicitly opt into verbose trace logs.
- Zoomed-out run nodes center their titles while preserving a larger,
  card-corner duration label.
- Failed DAG cards now show status only; inspect their retained logs for error detail.
- Retained run canvases now include a `Current workflow` control that returns
  directly to the live workflow view.
- Explorer collapse and restore controls now stay at the pane edge, and Explorer and inspector
  panes are independently resizable. Retained inputs, outputs, and traces use bounded progressive
  JSON with content-sized key columns, while logs use a record-separated continuous-text view
  without hidden unbounded DOM.
- Large-run hydration is now summary-first, cancellable, incrementally paged, and
  bounded across browser queues, descriptor windows, detail caches, and virtualized
  DOM rendering. `make web-bench` covers 10,000 retained runs in real Chromium.
- Added `ava operator --log-level` and explicit source-watcher and hot-reload
  lifecycle logs for successful, unchanged, and failed catalog refreshes.

### Operator transport

- Operator streams now replay bounded, typed run updates under an instance epoch
  and explicitly require a structural reset for stale cursors or restarts.
- Remote TUI state applies updates in sequence, ignores duplicates, and reloads
  structural run baselines instead of receiving complete run snapshots per event.
- Slow consumers receive an explicit reset from bounded stream queues, while
  summary refreshes preserve already-hydrated run details.
- Run selection paginates historical logs and agent events on demand. Trace
  hydration uses one lifecycle-owned worker and epoch/revision-guarded detail
  completions, with bounded backoff.
- The transport protobuf is not backward compatible with the previous full-state
  RPCs. Operators and remote TUI clients must upgrade together and regenerate
  bindings from the same protocol revision.
- Operator detail retention now enforces per-event, per-trace, per-node,
  per-run log count/byte, and aggregate per-run limits before accepting payloads.
- The TUI coalesces its cross-thread provider handoff in a bounded queue and
  schedules deterministic snapshot repair if sustained pressure drops detail.
- Caller-owned run IDs are limited to 256 UTF-8 bytes.
- Agent detail events retain a per-invocation source sequence and a separate
  transport cursor, so repeated calls to the same agent node do not discard
  later evidence when the source sequence restarts at one.

### TUI performance

- Virtualized log rendering keeps steady and incremental refresh work bounded by
  the visible viewport instead of rebuilding complete log history every frame.
- DAG pointer scrolling now accumulates higher-sensitivity horizontal and
  vertical targets with short animations instead of jumping between cells.
- Added `make tui-bench`, which enforces the 30 FPS refresh budget through
  10,000 log rows for steady and append scenarios.

### Worker execution services

- Added the versioned `ava.ExecutionServicesSpec` and worker-side
  `probe -> negotiate -> open -> materialize_input -> finalize/abort -> teardown`
  lifecycle for platform-managed task resources.
- Local and Ray executors carry service receipts separately from user payloads;
  terminal receipts are available through `RunHandle.execution_receipts()`.
- Input materialization happens in the consuming worker and may be eager or lazy.
  Failures do not silently fall back to ordinary execution.
- See [docs/execution-services.md](docs/execution-services.md).

### Awaitable workflow run handles

- Breaking: `Workflow.run(...)` now immediately returns a generic, awaitable
  `ava.RunHandle` instead of the workflow output. Use `.result()` to block or
  `await` the handle in asynchronous code.
- Run handles expose the synchronously allocated `run_id`, cached terminal
  output or failure, timeout-aware result access, and cooperative cancellation.
- Each embedded run uses one named non-daemon driver thread. Cancelling an
  asyncio waiter does not cancel the run, and active Python or Ray work is not
  forcibly interrupted.
- Handles are process-local lifecycle objects. Durable operator status,
  registries, persisted outputs, and recovery contracts are unchanged.

### Native agent support: bodyful `@ava.agent_step` and typed tables

- Pydantic `BaseModel` classes are now a first-class table schema source for
  `ava.IcebergTable(schema=...)` and `ava.LanceTable(schema=...)`: nested models
  become struct columns, lists become list columns, `Field(description=...)`
  becomes column documentation, and unmappable fields fail loudly with a
  per-field `ava.Json` JSON-string opt-out.
- Model-declared tables accept model instances (single or list) in `append(...)`
  and read back as validated models via `read_models()`.
- `ava.AppendResult` is generic over the row model and adds `to_models()`,
  `one()` (asserts exactly-one-row cardinality), and `one_or_none()`.
- `ava.ModelStream.one()`, `one_or_none()`, and `all()` inject validated row
  models at workflow stream boundaries with explicit cardinality contracts and
  contextual errors, while preserving passthrough, table-backed, Ray, and rerun
  behavior.
- New `ava.input.<field>` build-time placeholder for feeding validated run
  input into any node's arguments.
- Fix: futures passed explicitly as keyword arguments are no longer re-bound
  implicitly by position.
- See [docs/data-model-api.md](docs/data-model-api.md#define-a-schema).
- Added bodyful `@ava.agent_step` / `@ava.agent.step` aliases in the base
  package. Steps receive a callable `ava.Agent`, handle raw DSPy predictions in
  their own Python body, and explicitly persist their results.
- `ava.Signature` is a subclassable native DSPy contract using
  `ava.InputField()` / `ava.OutputField()`. The identical
  `ava.agent.Signature` also builds inline string signatures; skills and tools
  are configured only by `@ava.agent_step(...)`.
- `@ava.workflow(agent_defaults={...})` supplies workflow-scoped PredictRLM
  runtime defaults; agent-step kwargs override them. Process-global agent
  configuration and automatic agent-step table/output behavior were removed.
- `ava.agent.skills`, `ava.agent.Skill`, and `ava.agent.File` lazily re-export
  the corresponding PredictRLM APIs.
- The agent inspector now has Trace and Metadata tabs. Metadata is available
  before execution and shows the resolved signature, skills, static
  instructions, tools, and effective redacted runtime configuration. Expanded
  metadata fields follow their rendered order and include selectable scalar
  leaves.
- Fix: live agent evidence now remains attached across async node execution, so
  the Trace tab receives turns and status updates under local and Ray backends.
- Agent inspector object controls are always visible and recursively expand or
  collapse only the selected subtree without changing expansion state when the
  selection moves. Large collections remain lazily paginated. Trace durations
  use seconds, completed traces infer terminal status when older envelopes omit
  it, and custom model types retain useful declaration metadata instead of
  dropping the pane.
- See [docs/agent-steps.md](docs/agent-steps.md).

## 0.1.0-rc0

Initial team release candidate for Avalanche as a local-first Python data-flow
toolkit.

### What works

- Python flow authoring with `@ava.source`, `@ava.step`, `@ava.dest`, and
  `@ava.workflow`.
- Local execution with `ava.LocalExecutor`.
- Iceberg-backed and Lance-backed storage helpers.
- Canonical smoke-tested examples under `examples/`.
- Stream and Cursor examples using the current provider APIs.
- Local operator startup against explicit flow files or directories.
- Connected TUI mode through the operator gRPC API.
- Mock TUI mode for UI-only exploration.
- Bounded smoke gate with `make smoke-test`.
- Full pre-commit gate with `make precommit-check`.

### Known limitations

- APIs, operational behavior, and packaging details may change before a stable
  release.
- Operator and TUI commands are local-development paths, not deployment guidance.
- Production auth, authorization, TLS, and multitenancy are out of scope.
- One-click cloud deploy and schema migration CLI are not implemented.
- Durable operator replay/recovery is limited to the current implementation.
- `ava operator .` from the repository root is unsafe because discovery imports
  Python files; use a specific flow file or clean flow-only directory.
- Some tests may be skipped when optional local services or terminal features are
  unavailable.

### Team handoff

The official artifact for this release candidate is the Git repository. Start
with `README.md`.
