# Changelog

## Unreleased

- Keep input and output names whole on browser UI step cards; long types now wrap
  between words and show the full name and type on hover.
- **Upgrading** — This release makes agent steps runtime-neutral. Code that defines
  agent steps changes in two places, and the operator, its clients and
  `@trampoline-ai/operator-ui` must be upgraded together, because agent declarations,
  trace headers and trace bodies all change shape:

  ```python
  # before
  @ava.agent_step(Review, lm=MODEL, skills=[ava.agent.skills.docx])
  async def review(document: str, *, agent: ava.Agent) -> Report:
      prediction = await agent(document=document)
      return Report.model_validate(prediction.report)

  # after
  @ava.agent_step(Review, runtime=ava.runtimes.PredictRLM(lm=MODEL, skills=[ava.agent.skills.docx]))
  async def review(document: str, *, agent: ava.Agent) -> Report:
      return (await agent(document=document)).outputs.report
  ```

  The entries below give the details. The operator, client and TUI no longer import
  PredictRLM when they start; only the PredictRLM runtime, the `ava.agent.skills`,
  `Skill` and `DirectPythonBackend` re-exports, and the TUI's mock trace import it,
  on first use.
- Agents: add internal, not yet used models for running agent steps on runtimes
  other than PredictRLM: a runtime-neutral request, result and trace format. Agent
  steps behave exactly as before.
- Agents: PredictRLM now runs behind that runtime interface. Agent steps return the
  same PredictRLM prediction and send the same live events as before, with two
  visible differences: agent inputs are type-checked against the signature before
  the agent starts (a wrong type raises `AgentStepError`), and `ava.File` and
  `ava.Workspace` inputs appear in agent traces as their workspace reference, path,
  name, size and checksum (or a workspace's entry count, total size, or reference)
  instead of the mounted host path. The file bridge that mounts `ava.File` inputs
  and reserves `ava.File` outputs moved into the PredictRLM runtime unchanged. Agent
  options that `ava.runtimes.PredictRLM` has no field for are still passed unchanged
  to PredictRLM, which rejects an unknown one with its own `TypeError` on the first run.
- Operator UI: `make web-proto` now also generates zod schemas and TypeScript types
  for the runtime-neutral agent trace from the Python models
  (`scripts/export_agent_schemas.py`). The UI does not use them yet.
- Operator: agent step declarations (signature, tools, runtime settings) are now one
  typed model, validated once by discovery and again by the client, TUI and web UI.
  An agent step whose declaration cannot be built now fails discovery for its file
  with a diagnostic, instead of showing an error in the agent metadata panel. The
  declaration JSON has a new shape (PredictRLM settings, models and skills move under
  `runtime`), so the operator, its clients and `@trampoline-ai/operator-ui` must be
  upgraded together. Cached discovery results from older versions are rebuilt.
  The PredictRLM section lists the names of options passed through to PredictRLM
  without an Avalanche field (`extra_options`); their values appear in its settings
  only when they serialize to JSON.
  BREAKING-PROTOCOL: the Delta protocol moves to 2.0 (`avalanche.PROTOCOL_VERSION`)
  because the operator proto field 9 is renamed `agent_declarations_json` and its
  JSON shape changes; deployments built on protocol 1.x must be rebuilt.
- Operator: the agent trace summary sent to clients is now runtime-neutral: status
  (succeeded, failed or cancelled), runtime kind, trace format, model, total token
  usage and cost, turn and tool-call counts, and stop reason. Agent live-event
  summaries carry a common event kind next to the runtime's own kind, the turn, and
  tool-call and model-call counts. PredictRLM's sub-model, iteration limit and
  per-model usage are no longer in the summary; the web UI reads them from the
  trace body. The protobuf messages change, so the operator, its clients and
  `@trampoline-ai/operator-ui` must be upgraded together. BREAKING-PROTOCOL: part
  of Delta protocol 2.0 (trace header and agent activity fields replaced).
- **Breaking** — Agents: agent steps now take a `runtime=` instead of PredictRLM
  arguments. `lm=`, `sub_lm=`, `max_iterations=`, `skills=` and any other PredictRLM
  keyword move into `ava.runtimes.PredictRLM(...)`; passing them to
  `ava.agent.step(...)` or `@ava.agent_step(...)` raises a `TypeError` naming the
  replacement. Workflow defaults move the same way:

  ```python
  # before
  @ava.workflow(agent_defaults={"lm": MODEL, "max_iterations": 10})
  @ava.agent_step(Review, sub_lm=SUB_MODEL, skills=[ava.agent.skills.docx])

  # after
  @ava.workflow(agent_defaults={"runtime": ava.runtimes.PredictRLM(lm=MODEL, max_iterations=10)})
  @ava.agent_step(Review, runtime=ava.runtimes.PredictRLM(sub_lm=SUB_MODEL, skills=[ava.agent.skills.docx]))
  ```

  A step's PredictRLM settings are merged over the workflow default field by field
  (options passed through to PredictRLM merge the same way); with neither, the step uses `ava.runtimes.PredictRLM()`. `tools=` and
  `evaluations=` stay on the step. `ava.runtimes` is now importable from `ava`.
  Steps with `ava.File` or `ava.Workspace` fields still reserve their file outputs in
  the run workspace and reject `output_dir`. BREAKING-PROTOCOL: part of Delta
  protocol 2.0 (agent step arguments change).
- **Breaking** — Agents: `await agent(...)` now returns an `ava.agent.AgentResult`
  instead of the PredictRLM prediction. `result.outputs` holds the signature's output
  fields, already validated in strict mode, and `result.trace` is the invocation's
  runtime-neutral `ava.agent.AgentTrace` (header plus turns). Decorated steps now
  validate outputs like inline steps do: a missing or wrongly typed output raises
  `ava.agent.AgentOutputError` instead of reaching the step body.

  ```python
  # before
  prediction = await agent(document=document)
  return Report.model_validate(prediction.report)

  # after
  return (await agent(document=document)).outputs.report
  ```

  In evaluations, `ctx.output` is the validated outputs and `ctx.trace` is the full
  `ava.agent.AgentTrace` instead of a list of JSON events; a call whose outputs fail
  validation submits no evaluation. `ava.agent.AgentTrace` is no longer PredictRLM's
  `RunTrace`. Agent steps can now run on any agent runtime, not only PredictRLM.
  BREAKING-PROTOCOL: part of Delta protocol 2.0 (agent call results and evidence
  types change).
- Agents: a PredictRLM file output that has no host path, lies outside the step's
  reserved output folder, or is missing now raises `ava.agent.AgentOutputError`.
- **Breaking** — Operator: agent live events and final traces have a new,
  runtime-neutral body (schema version 2). Each live event carries Avalanche's own
  sequence number, the common event (a finished turn, a model or tool call, generated
  code) when there is one, and the runtime's own details as a typed payload. The final
  trace is the runtime-neutral trace record; its turns travel as live events. The
  operator validates every body once and rejects bodies from older versions, unknown
  formats, events whose runtime details do not match their common event, and a second,
  different final trace for the same agent call. The TUI and web UI show traces through
  one viewer per trace format. The operator, its clients and
  `@trampoline-ai/operator-ui` must be upgraded together.
- **Breaking** — Agents: listeners registered with `ava.agent.capture_agent_evidence`
  now receive the validated event models (`ava.agent.AgentEvidenceEvent`,
  `AgentTraceFinishedEvent`, `AgentTraceUnavailableEvent`) instead of dictionaries.
  BREAKING-PROTOCOL: part of Delta protocol 2.0 (agent evidence and the operator's
  agent event and trace bodies change; proto field 8 `telemetry_json` is reserved).
- Operator: the agent trace header no longer carries PredictRLM's telemetry
  reference; it is in the PredictRLM part of the final trace body.
- Agents: new `ava.runtimes.ClaudeAgent` runs agent steps with Claude through the
  Claude Agent SDK. Install the new `claude` extra (`avalanche-ai[claude]`) and the
  Claude Code CLI, then choose a model with `ClaudeAgent(model="claude-haiku-5-5")`.
  The CLI signs in with the first of `ANTHROPIC_API_KEY`, a subscription token from
  `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN`), and an existing `claude` login. Each call
  runs without the user's Claude settings, MCP servers or connectors; step tools are
  offered through an in-process tool server; outputs come back as structured output
  and are validated like every runtime's. One `ava.Workspace` input becomes the
  working directory. `ava.File` inputs are read where they are, by path, without
  copies; `ava.File` outputs are written to a folder reserved in the run workspace
  and come back as file references, as with PredictRLM. The TUI and web UI show Claude traces through their own viewer,
  and a Claude declaration shows its model and settings. Workflows that only declare
  Claude steps import and publish without the extra installed.
- Agents: every runtime now reports a trace turn in the same shape: `message` (what
  the model wrote that turn), `reasoning`, `tool_calls`, `duration_ms`, `usage` (the
  whole turn, every model summed) and `native`, the runtime's own part of the turn.
  PredictRLM's per-iteration `reasoning` field moves into `message`, matching
  Claude's reply text; `Turn.reasoning` now holds only a provider's own reasoning
  (such as Claude's extended thinking) and is `None` when the runtime does not expose
  it. Tool calls are the only shared action: `Turn.actions` becomes
  `Turn.tool_calls`, `ToolCallAction` becomes `ToolCall` (without a `kind` field),
  and the `code_execution` and `model_call` actions are gone. A PredictRLM turn's
  code, its output and whether it failed now sit in `turn.native` (format
  `predict_rlm.run/v1`), and each sub-model call is a `predict` tool call with its
  inputs as `arguments` and its outputs as `result`; `turn.native.predict_calls`
  gives each call's signature, model, usage and finish reason in the same order. A
  Claude turn's message ID, model, stop reason and API error sit in `turn.native`
  (format `claude_agent.messages/v1`), and a turn with an API error now counts as
  failed. Trace tool-call counts now include PredictRLM's `predict` calls.
  Code reading `ctx.trace.turns` in evaluations must use the new names; older stored
  traces are not converted.
- Live agent events: the shared `model.started`, `model.finished` and
  `code.generated` events, and `TurnFinished.omitted_model_calls`, are removed.
  PredictRLM still reports them as its own `predict.started`, `predict.finished`
  and `code.generated` events, so viewers keep showing running sub-model calls and
  code that has not finished. Operator protocol: agent activity descriptors no
  longer carry `model_call_count` (its field number is reserved), and the
  `MODEL_STARTED`, `MODEL_FINISHED` and `CODE_GENERATED` agent event kinds are
  reserved.
- Web UI and TUI: each turn shows its message first and keeps model reasoning in a
  collapsed "Model reasoning" section at the end, shown only when there is some.
  PredictRLM turns show their code and output from the turn's own part and list
  "Predict calls" with their inputs, outputs and details; Claude turns show the
  response's stop reason and API error.

## 0.15.1 — 2026-10-24

### Protocol

- Delta protocol 1.0 (baseline): `avalanche.PROTOCOL_VERSION`; see docs/compatibility.md.
- Re-export `avalanche.agent.DirectPythonBackend` so Delta reaches PredictRLM through Avalanche.

## 0.15.0 — 2026-10-24

- **BREAKING CHANGE:** the TUI and `ava tui` no longer support providers implementing
  only the legacy StateProvider protocol. Old data sources and compatibility paths
  are removed. Providers must implement TuiExtensions v1 and the complete revamped
  store contract, including stream reset/baseline/ACK and detail hydration. Incomplete
  providers fail at launch, rather than silently hiding controls.
- Rebuild the terminal workflow browser with viewport history and DAGs, bounded
  streaming logs, loaded-window search, node/channel/attempt filters, agent trace
  inspection, schedules, contextual help and a command palette.
- Add revision-pinned provider resource pages, JSON input preview/confirmation,
  capability-described actions, deep links and read-only mutation fences.
- Expose standalone operator input schemas, catalog versions, bounded server pages
  for history/schedules/artifacts, SHA-256-verified File-reference downloads and real
  worker stdout/stderr/log attempt metadata. Reconnect/reset belongs to the transport;
  health display no longer sends polling RPCs. Schedules remain inspection-only.
- Add a network-free, three-iteration JSON DummyLM agent example and prove its code,
  outputs and retained usage in the real operator's TUI trace screen.

### Known issues

- The wheel still exports generic top-level `runtime` and `tui` packages. Track the
  package-name collision follow-up in [#119](https://github.com/Trampoline-AI/avalanche/issues/119).

### 0.14.0

- Make `ava.File` a path-backed workspace reference: streaming read-only `open()`,
  `.path`, explicit `read_bytes()`, lazy SHA-256 and no file bodies on new wires.
  `File(content=...)`, byte inputs, `.content` and `ava.agent.File` warn and are
  removed next minor; migrate to `ava.File.from_path`.
- Tag File and reference Workspace wire values; reject unknown File targets.
  Untagged Workspace references warn for one release. Expose `ReferenceBinder`,
  `LocalReferenceBinder`, `rebind_arguments` and `file_reference_context` for hosts
  to reconstruct and bind every step's arguments against its declared types.
- Mount File/Workspace inputs at agent boundaries without copying. Reserve agent
  File outputs inside the run workspace per step/attempt and return public refs.
  Unsupported backends fail closed; DirectPython read-only mounts are advisory.
- Operator inputs/results now carry File references instead of file bodies. Fix
  reference Workspace decoding at the root and inside models, and CLI export of
  bound reference workspaces. CLI file export streams and input binding is lazy.
- Port file examples to the single public type. See [File references](docs/file-references.md)
  for wire examples, host binding signatures, temporary local lifetime and migration.
  This candidate pins unreleased PredictRLM PR #74; replace with its released
  version before merging.

- Keep shared connection trunks close to the intervening nodes, widening only
  where a wider row requires clearance instead of detouring around the whole graph.

- Place independent workflow inputs just before their first consumer in the graph,
  keeping supporting inputs beside the relevant preparation steps.

- Group outgoing workflow connections into shared trunks in both orientations,
  keeping adjacent nodes connected directly and branching longer connections
  from the midpoint between rows.

- Inspect nodes without zooming or recentering the canvas, and highlight their
  direct connections in blue while other edges become gray and dashed. Selected
  nodes keep a visible blue outline when zoomed out.
- Workflow graphs open in a horizontal layout on wide canvases and a vertical layout
  on narrow ones. The choice is made once when the graph loads; resizing the window
  or opening the inspector never changes it. A unified navpanel includes a toggle to
  switch between horizontal and vertical layouts.
- Remember the workflow sidebar’s open/collapsed preference locally. Show a clickable
  two-letter badge per workflow in the collapsed rail, with a 1px gray border and
  blue text for the active workflow.

## 0.13.0

- Releases: the operator UI package is now published to GitHub Packages only after
  the PyPI publish succeeds, so rejecting or failing the PyPI approval no longer
  leaves a UI release without a matching Python release.
- Security: update locked dependencies to patched versions: `certifi` 2026.10.19
  (drops root certificates whose intermediate keys were compromised), `aiohttp`
  3.14.5, `virtualenv` 21.14.12, and Vite 7.3.7 for the browser UI build. The
  browser UI now requires Vite 7.3.7 or newer.
- Remove LiteLLM after the compromised `litellm` 1.107.0 release. Require
  `predict-rlm>=0.10.0`, which uses DSPy 3.4.1's native engines, and exclude
  `litellm` from uv resolution here and in workspaces created by `init.sh`. Routes
  that DSPy serves only through LiteLLM (for example `bedrock/`) now need
  `dspy[litellm]`.

## 0.12.0

- Add directory-backed reference workspaces for host-managed storage; see 0.11.0.
- Contributors: project agent skills in `.agents/skills/` are tracked again, and
  `.claude` links to `.agents` so Claude Code loads the same skills, starting with
  `release-prep`.
- Breaking: Avalanche now requires Python 3.13. Python 3.11 and 3.12 were never
  tested in CI, and the `delta` CLI used to deploy workflows to Delta Cloud
  requires 3.13. Projects on older Python versions stay on Avalanche 0.11.x.
- The starter project from `ava init` now uses Python 3.13, requires
  `avalanche-ai>=0.11`, and installs the `delta` CLI in its `dev` dependency
  group, so `uv run delta deploy` works without a separate install. Its
  `AGENTS.md` documents the sign-in, deploy, and run commands.

## 0.11.0

- Add `Workspace.from_directory(path, reference=...)` to use an existing absolute
  directory without copying its contents. Reference workspaces keep their host-owned
  lifetime across node success and failure; value workspaces are unchanged.
- Serialize reference workspaces as a versioned, opaque reference without paths or
  file contents. Deserialized references are unbound until the host supplies a directory.
- Document both workspace forms and add a repeatable two-step peak-RSS benchmark
  comparing reference and value workspaces with a 200 MB file.

## 0.10.0

- Inline agent nodes accept `evaluations=` too:
  `ava.agent.step(Signature, inputs=..., evaluations=ava.Evaluations(...))` inside a
  workflow now records operator evaluations for that node, the same way decorated
  agent steps do. Before, the argument was passed on to PredictRLM as an unknown
  setting and no evaluation ran.
- Agent steps that use Codex LM now show their model as `codex/<model>` instead of
  `openai/<model>` in the operator UI's definition and run views, and in run traces
  sent to evaluations. Codex LM sends requests through a ChatGPT login, not the
  OpenAI API, so the old label named the wrong provider. Cost tracking is unchanged.
- Agent examples now choose their models from what is set up: Codex LM first, then
  OpenAI when `OPENAI_API_KEY` is set, then Anthropic Claude when `ANTHROPIC_API_KEY`
  is set. Keys in the project `.env` count, and per-example model variables still
  override the choice. The customer feedback example no longer needs a Gemini key
  for its sub-model.
- Composite score summaries use centered dots. DAG nodes show the first three
  scores plus a black `and N more` label, smaller only when zoomed in. Sidebar
  headers show every score and wrap as needed.
  Scores now refresh when evaluations finish after the run, including embedded
  workspaces with hidden or unchanged custom graph controls.
- `Evaluations` now validates declarations through Pydantic and exposes read-only
  metric/composite mappings. Invalid fields raise field-specific `ValidationError`s;
  synchronous-callable checks and independent evaluation execution are unchanged.
- Evaluation metrics now group selector source paths under a collapsed **Input**
  section instead of separate source badges. Paths such as `input.packet`,
  `output.review.summary`, `output`, and `trace` distinguish agent-call arguments,
  named prediction fields or the full prediction, and the invocation trace.
- Trace source paths use the agent accent color inside evaluation Input sections,
  distinct from neutral input and output paths. In run views, trace labels are
  shortcuts that underline only on hover. Click, Enter, or Space opens and focuses
  the same node's Trace tab.
- Native agent evaluations now capture the first successful agent call to return:
  its actual arguments, complete DSPy prediction, and only that invocation's trace.
  Step return values and postprocessing remain separate; later calls run normally
  without replacing or resubmitting evaluation, even if step postprocessing fails.
  Full PredictRLM 0.9 iteration details remain available to evaluations without
  resending them through the operator's live trace channel.
  Nested agent steps keep separate capture state, so unevaluated helpers—including
  inline agents—cannot claim the enclosing step's evaluation.
  Evaluation snapshots also retain workflow-defined selectors and output classes
  when the project lives under Python's installation prefix, without copying
  installed libraries.
- Workflow discovery no longer eagerly loads DSPy through evaluation capture,
  avoiding unnecessary startup work during scans and hot reloads.

## 0.9.0

- Added inline `ava.agent.step(Signature, ...)` calls for direct workflow chains,
  with typed inputs, validated outputs, and the same skills, tools, and model
  settings as decorated agents.
  Both call forms are covered in the mirrored workflow-author API reference.
- Agent decorators remain supported outside workflows. Nested agent decorators
  are rejected; `inputs=` and `slug=` are only supported for inline calls.
- Fixed inline agent chains unpacking a single list- or tuple-valued output into
  separate downstream arguments. Empty and nonempty collections now retain their
  signature field boundary; ordinary Python nodes keep their existing behavior.

- Updated locked Python and browser-tooling dependencies to address 15 known
  security findings, including a coordinated fsspec/s3fs/AWS-client upgrade.
  DiskCache remains unpatched upstream.

- Added a workflow-author API reference for step decorators, graph operations,
  runs, inputs/context, files/workspaces, agents, classifiers, Iceberg/Lance
  storage, streams, cursors, and CLI commands. The workflow primitive list links
  to dedicated agent and classifier references. Topic pages under `docs/api/`
  are mirrored in `skills/avalanche/references/api/` for offline lookup; framework
  internals, operator protocols, and browser embedding are outside this reference.
  Clarified asynchronous CLI validation, Local/Ray argument binding, supported
  input/context overrides, and the limits of stream acknowledgment and Lance deletion.

## 0.8.0

- Upgraded PredictRLM to 0.9.0.
- Added `ava.agent.AgentTrace`, the same type as PredictRLM's `RunTrace`.

## 0.7.2

- `import avalanche` no longer imports Polars or PyArrow. The public types,
  storage, lineage, model-frame, and stream modules bind them lazily and load
  them on first use, so a process that only uses the DAG, runtime, and
  pydantic-typed I/O never loads the dataframe stack or starts its native
  worker threads (OpenBLAS, jemalloc, the Polars pool). Type-checker views are
  unchanged; every frame-accepting or frame-returning API behaves as before.

## 0.7.1

- `import avalanche` no longer eagerly loads the Iceberg and Lance storage
  backends. The `Iceberg*` and `Lance*` names exported from the package (and
  the `avalanche.iceberg` / `avalanche.lance` submodules) are resolved on first
  attribute access, the same way `avalanche.agent` already was. Programs that
  only use the DAG, executor, and runtime API skip importing pyiceberg's catalog
  stack, pyarrow, pandas, SQLAlchemy, and the object-store clients, which
  accounted for roughly three quarters of the package import time. Explicit
  `from avalanche.iceberg import ...` / `from avalanche.lance import ...`
  imports, `from avalanche import IcebergTable`, and `ava.IcebergNs` attribute
  access are unchanged.

## 0.7.0

- Replaced the built-in REST API's handwritten routing with FastAPI and moved
  the shared browser/REST listener to Uvicorn. Both are runtime dependencies.
  Existing `/api/v1` endpoints still call the operator's gRPC service;
  interactive docs and OpenAPI are available at `/api/docs` and `/api/openapi.json`.
  Malformed HTTP framing is rejected by Uvicorn before API validation.
  Method-not-allowed responses list every supported method for the requested REST path.

## 0.6.2

- The operator UI keeps the full workflow DAG visible while a run is requesting,
  with dimmed, disabled nodes and a loading indicator until its run snapshot is ready.
- Removed the documentation website source files from this repository now that
  they are maintained separately.
- Added native agent-step evaluations: select final return values, bound inputs,
  or all agent-call traces with `ava.Metric`, attach `ava.Evaluations`, and combine
  TypeSafe answers into explicitly normalized scores. Operator-managed evaluations
  run independently of workflow success, with per-execution pending/results/errors
  visible in the browser and available through gRPC and REST. Records are local,
  in-memory state, not restart recovery. Includes a real-agent example and
  illustrated authoring guidance.
- Expanded the evaluations example into a no-input incident-handoff workflow:
  generate synthetic operational evidence, prepare a cited brief with a real
  agent, and inspect five quality metrics plus a normalized composite.
- Evaluation declarations are now visible before execution: graph nodes show a
  metric-count badge, and the current-definition inspector shows questions,
  criteria, composite names, and effective Jev settings. Historical run graphs
  retain their original declarations after workflow edits.
- Centered compact graph node names independently of the evaluation badge, so
  configured agent labels remain centered on their cards.
- Split the current agent inspector into Agent definition, Evals, and Step
  definition tabs, separating evaluation declarations and the step interface
  from the rest of the agent configuration.
- Removed the step interface card from agent run **Run I/O**; it now shows only
  recorded agent inputs and outputs. The current **Step definition** tab keeps
  the interface card, and other step inspectors are unchanged.
- The incident-handoff example sends source facts and observable agent actions
  for trace inspection instead of duplicate full-trace copies, avoiding a
  TypeSafe context-limit error on the documented path. Classifier responses now
  allow rounding differences between Score and its reported probabilities, and
  HTTP 400 context-limit errors identify `max_tokens_exceeded` without logging
  the private request body.
- Browser classifier and evaluation results now display the values validated by
  Python without repeating score, probability, composite, or lifecycle checks
  in the consumer.
- Simplified the agent run evaluation tab to show one result directly, without
  a numbered outer card, repeated heading, or boxed metric cards.
- Completed evaluation composites now appear beside run-node badges and in the
  agent sidebar header as one-decimal percentages, showing one named score or
  up to three scores in declaration order. Both surfaces share run-level polling.
- Evaluation metrics now show Trace, Output, and Input source labels with selected
  field paths derived from Python selector syntax without executing selectors or
  exposing raw evidence. Opaque selectors show their callable identity.
- Composite and Noul percentages use a red–yellow–green value scale, with colon-separated
  composite summaries and matching node/sidebar label colors. Compact nodes show
  percentages without names and icon/count evaluation pills at the duration's size.
- The evaluation tab places Composites above Metrics as equally styled, unboxed
  sections. Composite tab values match node/header percentages and colors. Choice
  results list probabilities once with only the winner bold and turquoise;
  other choice percentages, score-level probabilities, and confidence stay neutral.
- Run inspector composite scores are left-aligned beneath the step title.
- Compact DAG nodes enlarge the Agent label and its icon to match the evaluation
  pill's text and icon sizes; detailed nodes keep their existing sizing.

## 0.6.1

- `ava web` now exits cleanly when Ctrl-C arrives during browser listener startup.
  If upstream gRPC initialization fails or is interrupted after the HTTP socket
  opens, the listener is closed instead of leaking its port.

## 0.6.0

- Added a built-in development REST API for flow discovery, run creation,
  summaries, snapshots, cancellation, output, and activity through the existing
  operator gRPC service. `ava operator` now serves the browser UI and REST API
  on loopback port 7435 by default; `--web-port` changes it and `--no-web`
  preserves gRPC-only operation. `ava dev` and `ava web` expose the same API.
- REST mutations require JSON content types, including cancellation, so ordinary
  cross-origin browser form submissions cannot cancel local runs.
- `ava operator` and `ava dev` keep cleanup non-interruptible by SIGINT/SIGTERM,
  releasing owned resources and restoring signal handlers even after repeated
  shutdown signals or a fatal operator failure. Startup rollback uses the same
  protection, so signals during a gRPC bind failure or operator initialization
  failure do not leak services or hide the original error.

## 0.5.4

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
- Added bodyful `@ava.agent_step` and `@ava.agent.step` decorator spellings in the
  base package. Steps receive a callable `ava.Agent`, handle raw DSPy predictions
  in their own Python body, and explicitly persist their results.
- `ava.Signature` is a subclassable native DSPy contract using
  `ava.InputField()` / `ava.OutputField()`. The identical
  `ava.agent.Signature` also builds inline string signatures; skills and tools
  are configured at each agent-step declaration.
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
