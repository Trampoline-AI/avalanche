## 1. Versioned run topology

- [ ] 1.1 Add an immutable executed-topology model and retain it from the run worker's prepared workflow metadata when creating a run.
- [ ] 1.2 Include the retained topology in in-memory structural snapshots and the operator protocol without materializing workflow objects across process boundaries.
- [ ] 1.3 Update transport conversion and clients to hydrate a run from its topology snapshot plus execution state.
- [ ] 1.4 Add regression coverage proving a completed and active run remain renderable after nodes or edges change and after the workflow is removed.
- [ ] 1.5 Preserve bounded structured agent invocation inputs and terminal outputs in existing evidence, encode nested PredictRLM `File` values as tagged paths, and verify input, output, list, unsupported, over-limit, and worker-to-operator behavior.
- [ ] 1.6 Retain bounded worker-provided node failure messages in node state, snapshots, live updates, protocol conversion, and run inspection with focused regression coverage.
- [ ] 1.7 Decompose exportable `RunTrace` into a lightweight header, paginated rich event/turn descriptors, and complete on-demand `IterationStep` bodies; migrate TUI hydration away from monolithic `ReadTrace` and verify semantic coverage and bounded reads.

## 2. Atomic catalog reload and live updates

- [ ] 2.1 Change discovery refresh to validate a candidate catalog, retain the last valid catalog on failure, and expose reload diagnostics.
- [ ] 2.2 Add typed scan-target catalog metadata with alias, normalized target path, and file/directory kind to initial reads, live replacements, reset baselines, protocol conversion, and clients.
- [ ] 2.3 Add a monotonic catalog revision and a full catalog-update event emitted after each successful replacement.
- [ ] 2.4 Replace the run-only update stream/envelope with an operator-update stream that carries run updates, catalog revisions, and reset notices; regenerate protobuf bindings.
- [ ] 2.5 Migrate the Python operator client and TUI provider to the new stream and authoritative reset baseline behavior.
- [ ] 2.6 Add focused operator and client tests for scan-target grouping and created, changed, removed, failed, replayed, and reset catalog states.

## 3. gRPC-Web delivery

- [ ] 3.1 Add an optional loopback-default browser listener within the operator process that serves compiled web assets and adapts gRPC-Web unary and server-streaming calls to the shared authoritative `Operator` instance without a required sidecar.
- [ ] 3.2 Add the React, TypeScript, and Vite frontend build; generated TypeScript stubs from `operator.proto`; `@xyflow/react`, CodeMirror 6, and `@tanstack/react-virtual`; package data; and development/production asset-loading paths.
- [ ] 3.3 Add `ava` command and operator configuration support for launching and reporting the local web UI endpoint without weakening non-loopback safeguards.
- [ ] 3.4 Add integration coverage for browser-compatible unary calls, live stream delivery, loopback binding, and static asset serving.

## 4. Web UI

- [ ] 4.1 Implement ephemeral catalog and run projections from authoritative operator updates, with stream reconnection, reset reconciliation, and non-authoritative start/cancel request state.
- [ ] 4.2 Implement the scan-target Explorer with workflow/run hierarchy and workflow-versus-run navigation.
- [ ] 4.3 Implement the current-workflow blueprint canvas with pan/zoom, one dependency arrow per source-target pair, agent field lists inside cards, and declaration inspection.
- [ ] 4.4 Implement the historical-run canvas with its topology snapshot, execution-focused node cards, status, duration, failure, logs, and a visible distinction from current workflow state.
- [ ] 4.5 Implement virtualized, paginated `RunTrace` inspection with header metadata, chronological turn summaries, selected complete turn details, live following, errors, and a bounded LRU detail cache.
- [ ] 4.6 Implement separate agent Inputs and Output views using retained invocation evidence and declaration field metadata.
- [ ] 4.7 Render tagged PredictRLM file values within ordinary agent Inputs and Output views using path-specific presentation without copying, storing, or validating files.
- [ ] 4.8 Implement run start with a closed-by-default schema-blind JSON-object editor, authoritative validation errors, and active-run cancellation using generated gRPC-Web clients.
- [ ] 4.9 Add browser-level tests covering Explorer navigation, current workflow rendering, a live reload, a historical topology mismatch, complete demand-loaded trace inspection, bounded hydration/cache behavior, agent inputs and outputs, file path values, stream reset recovery, default no-input run start, optional JSON input, validation errors, and cancellation.

## 5. Verification and documentation

- [ ] 5.1 Run focused operator, protocol, TUI-client, adapter, and browser test suites; add an end-to-end local operator reload scenario.
- [ ] 5.2 Update local development and operator documentation with web UI launch, loopback exposure, reload semantics, and the distinction between workflow and run views.
