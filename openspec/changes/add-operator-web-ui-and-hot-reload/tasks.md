## 1. Versioned run topology

- [x] 1.1 Add an immutable executed-topology model and retain it from the run worker's prepared workflow metadata when creating a run.
- [x] 1.2 Include the retained topology in in-memory structural snapshots and the operator protocol without materializing workflow objects across process boundaries.
- [x] 1.3 Update transport conversion and clients to hydrate a run from its topology snapshot plus execution state.
- [x] 1.4 Add regression coverage proving a completed and active run remain renderable after nodes or edges change and after the workflow is removed.
- [x] 1.5 Preserve bounded structured agent invocation inputs and terminal outputs in existing evidence, encode nested PredictRLM `File` values as tagged paths, and verify input, output, list, unsupported, over-limit, and worker-to-operator behavior.
- [x] 1.6 Retain bounded worker-provided node failure messages in node state, snapshots, live updates, protocol conversion, and run inspection with focused regression coverage.
- [x] 1.7 Decompose exportable `RunTrace` into a lightweight header, paginated rich event/turn descriptors, and complete on-demand `IterationStep` bodies; migrate TUI hydration away from monolithic `ReadTrace` and verify semantic coverage and bounded reads.
- [x] 1.8 Replace retained run declaration metadata with input/output field schemas containing only names, types, and descriptions; keep full declarations in the current catalog.
- [x] 1.9 Update topology transport and browser parsing for the schema-only projection, with regression coverage proving instructions and execution configuration are absent.

## 2. Atomic catalog reload and live updates

- [x] 2.1 Change discovery refresh to validate a candidate catalog, retain the last valid catalog on failure, and expose reload diagnostics.
- [x] 2.2 Add typed scan-target catalog metadata with alias, normalized target path, and file/directory kind to initial reads, live replacements, reset baselines, protocol conversion, and clients.
- [x] 2.3 Add a monotonic catalog revision and a full catalog-update event emitted after each successful replacement.
- [x] 2.4 Replace the run-only update stream/envelope with an operator-update stream that carries run updates, catalog revisions, and reset notices; regenerate protobuf bindings.
- [x] 2.5 Migrate the Python operator client and TUI provider to the new stream and authoritative reset baseline behavior.
- [x] 2.6 Add focused operator and client tests for scan-target grouping and created, changed, removed, failed, replayed, and reset catalog states.

## 3. gRPC-Web delivery

- [x] 3.1 Add an optional loopback-default browser listener within the operator process that serves compiled web assets and adapts gRPC-Web unary and server-streaming calls to the shared authoritative `Operator` instance without a required sidecar.
- [x] 3.2 Add the React, TypeScript, and Vite frontend build; generated TypeScript stubs from `operator.proto`; `@xyflow/react`, CodeMirror 6, and `@tanstack/react-virtual`; package data; and development/production asset-loading paths.
- [x] 3.3 Add `ava` command and operator configuration support for launching and reporting the local web UI endpoint without weakening non-loopback safeguards.
- [x] 3.4 Add integration coverage for browser-compatible unary calls, live stream delivery, loopback binding, and static asset serving.

## 4. Web UI

- [x] 4.1 Implement ephemeral catalog and run projections from authoritative operator updates, with stream reconnection, reset reconciliation, and non-authoritative start/cancel request state.
- [x] 4.2 Implement the scan-target Explorer with workflow/run hierarchy and workflow-versus-run navigation.
- [x] 4.3 Implement the current-workflow blueprint canvas with pan/zoom, one dependency arrow per source-target pair, agent field lists inside cards, and declaration inspection.
- [x] 4.4 Implement the historical-run canvas with its topology snapshot, execution-focused node cards, status, duration, failure, logs, and a visible distinction from current workflow state.
- [x] 4.5 Implement virtualized, paginated `RunTrace` inspection with header metadata, chronological turn summaries, selected complete turn details, live following, errors, and a bounded LRU detail cache.
- [x] 4.6 Implement separate agent Inputs and Output views using retained invocation evidence and declaration field metadata.
- [x] 4.7 Render tagged PredictRLM file values within ordinary agent Inputs and Output views using path-specific presentation without copying, storing, or validating files.
- [x] 4.8 Implement run start with a closed-by-default schema-blind JSON-object editor, authoritative validation errors, and active-run cancellation using generated gRPC-Web clients.
- [x] 4.9 Add browser-level tests covering Explorer navigation, current workflow rendering, a live reload, a historical topology mismatch, complete demand-loaded trace inspection, bounded hydration/cache behavior, agent inputs and outputs, file path values, stream reset recovery, default no-input run start, optional JSON input, validation errors, and cancellation.

## 5. Verification and documentation

- [x] 5.1 Run focused operator, protocol, TUI-client, adapter, and browser test suites; add an end-to-end local operator reload scenario.
- [x] 5.2 Update local development and operator documentation with web UI launch, loopback exposure, reload semantics, and the distinction between workflow and run views.

## 6. Dogfood remediation

- [x] 6.1 Prevent unchanged watched sources or semantically unchanged discovery results from advancing the catalog revision or publishing replacement updates, with focused watcher and operator regression coverage.
- [x] 6.2 Make the healthy desktop workspace and workflow canvas consume the available viewport instead of collapsing to content height.
- [x] 6.3 Preserve Explorer workflow/run navigation, selected-view titles, primary controls, and bounded pannable canvases at 375 CSS pixels and wider.
- [x] 6.4 Give the CodeMirror workflow-input editor a descriptive accessible name and add an accessibility regression assertion.
- [x] 6.5 Update secondary metadata colors to meet WCAG 2.2 Level AA contrast in populated workflow and run views.
- [x] 6.6 Give repeated node invocations distinct visible labels and accessible names derived from stable node identity.
- [x] 6.7 Correct the retained-output empty-state grammar.
- [x] 6.8 Add browser regression coverage for desktop sizing, narrow navigation and canvas bounds, repeated invocation identity, accessible input naming, empty-state copy, and automated contrast checks.

## 7. Large-run performance remediation

- [x] 7.1 Add an atomic latest selected-run snapshot RPC and immutable forward/newest-first log and agent-event pagination contracts, including node-filtered logs and epoch-correct detail tokens.
- [x] 7.2 Implement operator, native gRPC, gRPC-Web, and Python-client support for the new snapshot and paging contracts without changing exact retained-baseline semantics.
- [x] 7.3 Replace browser page-draining APIs with cancellable single-page methods, summary-only baseline hydration, one demand-loaded selected snapshot, and distinct JSON/text detail readers.
- [x] 7.4 Bound ordered browser update batching, pending queues, live descriptor tails, and repair watermarks while preserving reset and exact sequence reconciliation.
- [x] 7.5 Migrate App and Explorer to summary-backed navigation and cancellable selected-run snapshot loading without browser-owned lifecycle state.
- [x] 7.6 Make inspector hydration active-tab-only, generation-scoped, cancellable, incrementally paged, node-filtered, and virtualized; isolate Trace following from Inputs, Output, and Logs.
- [x] 7.7 Make parsed detail caching byte-bounded and render large nested values through accessible collapsed, depth-limited, and chunked expansion.
- [x] 7.8 Contain Explorer and graph rerenders so unrelated log, event, and detail updates do not rebuild navigation or graph layout.
- [x] 7.9 Add deterministic high-volume protocol, state, inspector, DOM-bound, cancellation, decoding, and browser performance regression coverage.
- [x] 7.10 Run focused and aggregate Python, TUI, browser build/test/benchmark, smoke, and real-browser large-run verification.
