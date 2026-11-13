# Changelog

## 0.20.13

- Operator UI: `MachineInstance.pending_events[].payload` is optional, for hosts
  that keep event payloads out of the run read (Delta).

## 0.20.12

- Renamed: a machine "visit" is now a **state entry**. `ava run result --visit N`
  is `--entry N`, `ava run retry RUN_ID --visit` is `--entry`, result documents
  carry `entry`, step IDs are `e2.step`, SDK types are `StateEntry*` and the
  operator UI shows "Entry N". The local operator's state database migrates once
  and drops machine runs recorded before the rename (flow runs are kept).
- Local operator: a machine `send` whose payload validator crashes or times out
  is accepted and queued instead of failing with a server error. The run shows
  the validation failure, and the event is delivered once the validator works.
- Operator UI Timeline: an event delivered after being deferred in a state entry
  that is not loaded (an older history page) now shows as delivered, noting it
  was deferred in an earlier entry, instead of disappearing.

## 0.20.11

- The operator UI's schema view reads types the way they are written:
  `string | null`, `Address | null`, `list[Address]`, `"aisle" | "window"`,
  `dict[string, integer]`. Alternatives no longer appear as `Union` with
  `anyOf 1` / `anyOf 2` rows; an optional model opens straight onto its fields.
  A generated title that only repeats the field name is hidden, and a field's
  `default` is separated from its value. The input field list uses the same
  notation.

## 0.20.9

- Store local flow and machine keys with the shared UTF-8 key serializer, and
  migrate older ASCII-escaped identities without changing their run IDs or receipts.

- Use one portable run-key contract on the SDK test host, local operator and
  deployed hosts. UUIDs, dates, UTC datetimes, enums, tuples, sets/frozensets,
  nested plain models and plain unions now share typed normalization; unordered
  values are sorted independently of Python's hash seed. Existing supported key
  bytes and manifests without typed key declarations remain readable.
- **Breaking:** workflow/machine key fields with custom validators, serializers,
  aliases/alias generators, default factories or unsupported types/configuration
  are refused at definition time with the field name. Move business normalization
  before the start operation and pass a plain portable identity field instead.
- Graceful operator shutdown no longer sends run-cancellation requests. Parked
  calls recover the same original child after SIGINT/SIGTERM as after a crash;
  explicit run cancellation still cascades only to children the parent created.
- Scheduler idle logs report occupied executor slots, host-operation futures,
  parked waits and the coordinator PID. Flow waits (including calls) still retain
  a coordinator process; machine instances between visits do not.

- Add declared `ava.call(target, input, timeout=..., when=...)` for native flow
  and machine children. Calls wait without occupying an executor worker and return
  the typed final output, with File/Workspace references unchanged. Child failure,
  cancellation and timeout fail the call; timeout leaves the child running.
- Persist call waits and parent/child start receipts in the local operator. A
  parked parent reattaches after restart; created children alone receive parent
  cancellation. Refuse self/cyclic waits and capture target names lazily.
- Inspect native run links with `ava run links RUN_ID --json` or
  `GET /api/v1/runs/RUN_ID/links`. The Delta port is separate.
- The operator UI run page shows the run tree under the run list, the same for
  flow and machine runs: parents, the run and its children with status and time
  ago. Call and start nodes link to their child run; rows open runs of other
  flows and machines. A step's Run I/O output (flow steps and machine visit
  steps alike) reads the accepted typed result from the shared result route,
  falling back to the bounded preview.

- Read typed flow, machine-final, visit and step results through one versioned
  reference-only result model. The local operator retains completed non-final
  visit outputs across restarts; `ava result` (`ava run result`) and
  `delta runs result` accept `--visit` and `--node` selectors.
- Bare set and frozenset results serialize as canonical ordered lists instead
  of raising a result-publication TypeError.
- Delta console archived machine steps retain the shared step input/output
  inspector by resolving their immutable attempt observations.
- Machine visits keep the same local workspace folder across visits and operator
  restarts, so later visits see earlier edits. The test host binds Workspace.path
  the same way as flow steps.
- Local child starts and public/declared machine sends carry the same File and
  Workspace references flows use, without restaging, exporting or copying an
  existing reference. Deferred events validate against their declared receiving
  models before acceptance, just as current-edge events do.

## 0.20.7

- Operator: new keyed flow and machine starts reuse normalized key values before
  strict full-input validation, matching key lookup and declared-send coercion.
  Existing keys still ignore replacement non-key input and resources.

- Flow-to-machine `ava.start_run` and declared machine sends work again on
  Delta (scheduler-owned, no tenant credentials), matching the local operator.
- Delta's CLI retires `delta machine start/show/list`; start machines with
  `delta runs start` and inspect them with `delta flows`/`delta runs`, which
  show the same flow/machine kind as `ava flow` and `ava run`.

- CLI: `ava flow list` and `ava flow show` (`get` is equivalent) inspect flow
  and machine definitions, including machine states and key fields. Definition
  and run lists show a TYPE column; detail shows Kind. JSON includes `kind`
  (`flow` or `machine`), derived from stored metadata for historical runs.
- CLI: `ava run list/show` now default to readable text; use `--json` for
  machine-readable output. `ava run history RUN` pages machine transitions.
  Send by run ID with `ava machine send EVENT --run RUN`, or by named key
  fields with `ava machine send MACHINE EVENT --key FIELD=JSON`. The run-ID
  form no longer takes a redundant machine name.

## 0.20.5

- Operator: run and visit history recorded before a restart remains readable in
  Timeline; returned cursors use the current instance without accepting stale
  client cursors from the previous instance.

- **Operator UI: run Timeline tab.** The bottom pane of every run page has two
  tabs, Logs and Timeline. Timeline is one time-ordered list of the run's
  events: visits entered and left, steps (with retries), events accepted,
  deferred, ignored, dropped or delivered with their send receipts, timers
  armed, fired or cancelled, waits, cancel and outcome; flow runs list their
  steps, waits and outcome in the same tab. Hovering an entry highlights its
  state and arc (or step) on the graph; picking a state or step on the graph
  filters the list. Pending timers, deferred events and open waits (with the
  decision form) stay pinned on top. The Visits and Waits sections of the
  runs pane are gone, and so are the exported `MachineRunWaits`,
  `MachineVisitWaits` and `MachineVisitTimeline` (use `RunTimeline`). Waits are
  read on run changes instead of a fixed 1.5 s poll.
- Operator UI: the bottom pane's Logs and Timeline tabs keep static labels and
  widths; the pane's scope shows in a chip above its content ("All steps",
  "Step: apply_nits · visit 2", "All states", "State: amend") with a clear
  button, replacing the timeline's "Showing … · Show all" row.
- Operator UI: every Timeline row has one time column, labeled "+ since start"
  (hover for the local time; "—" says why a time is unknown), rows read as
  sentences ("Step apply_nits ran in amend (visit 2) · ok · took 0.2s"), and a
  finished step, visit or wait shows how long it took on one row.
- Operator UI: the runs panel is titled **Runs** (was Timeline; its expand and
  collapse actions read "Expand runs" / "Collapse runs"). Run key labels keep
  their full width in the list; the date after them truncates instead, and the
  row tooltip names both. Machine arc labels scale up to stay readable at the
  fitted zoom. Machine run logs from other visits read "step · visit N" and
  open that visit's step.
- Operator UI: the open Decide panel shows the decision's reviewed artifact
  above the form, and a resolved wait's Timeline entry shows its audit outcome
  beside the reason. Show all (and a run or flow change) also clears the state
  selected on the graph. Countdowns read in minutes and hours ("9m 26s left")
  on both the timer arc and the pinned row. Steps inside a visit are named
  without the `v<N>.` prefix. On narrow canvases the Timeline tab no longer
  overlaps the record count, the snapshot banner is hidden rather than drawn
  under the runs panel, and the graph keeps clear of the canvas controls.
- Operator UI: on a run page narrower than 900 px the runs panel folds to a
  one-line chip ("Runs · acme/api #45 ▸") so the graph is fitted into the
  whole canvas; the chip unfolds it, its header folds it again, and the choice
  is remembered with the rest of the workspace layout.
- **Local operator storage upgrade:** opening an existing `state.db` upgrades it
  one-way to schema v8; older operator versions cannot reopen that database.
  Back up the database before upgrading. Durable writes use SQLite WAL with
  `synchronous=FULL` (including admission and transitions before replies).
- `ava state delete` and `ava state clear` remove retained keyed-run history but
  keep permanent key tombstones. Starting with the same key returns the original
  run with `read_state=expired`, not a new execution. Use a new key to start anew.
- Active-run wait controls return HTTP 503 when their coordinator is temporarily
  unavailable, rather than reporting a misleading missing run or decision.
- **CLI name collision:** `ava run list|show|retry|cancel|rerun` are now reserved
  command words and shadow workflows with those names. Rename the workflow or
  invoke it using its fully qualified workflow selector (`file.py::symbol`).
- Temporary local machine definition loss leaves waiting runs repairable:
  `definition_unavailable` and its error are visible in the run's event facts;
  sends remain durable and timers retain their deadlines. Restoring the source
  resumes the same run/key on send, timer delivery, explicit retry or restart.
  Definition reloads are source-scoped and coalesced, with a shared one-second
  minimum interval. Concurrent commands share the next allowed refresh; newer
  repair commands wait in the queue without blocking the owner. With watching
  off, timer retries back off per machine from one to 60 seconds; watching
  instead wakes waiting runs on catalog reload.
  Original timer deadlines never move and overdue timers fire once on recovery.
  Sends, explicit retry and restart bypass timer backoff. A successful definition
  read is always usable, even during retry backoff; closed runs and removed
  catalog entries no longer leave stale recovery bookkeeping behind.
  Only a loaded definition without the current state produces `state_missing` /
  `needs_attention`; restore that state and use `ava run retry RUN_ID --visit`.
  Visit preparation/start failures preserve the journal and require explicit
  visit retry; failed, unacknowledged keyed-flow starts release the key.
- Deferred event validation treats timeout and source-lookup races as temporary
  definition loss, with one queued recovery wake and one-to-60-second backoff.
  A validation child exiting nonzero or by signal is retried at most three times
  per queued receipt; the durable failure count survives restart. The visible
  `definition_unavailable` facts report `validation_retry_limit`; the event stays
  queued. Repair the source and explicitly retry the visit to reset that budget.
- Explicit machine visit retry now fails with `definition_unavailable` or
  `state_missing` when repair cannot start, instead of reporting a successful
  no-op. CLI, REST and gRPC expose the reason without advancing the visit;
  interrupted runs retain their unknown-outcome protection. Sends queued against
  an interrupted run whose loaded state is absent record `state_missing`.
- Local machine sends remain JSON-only until durable artifact-event ownership
  is implemented: admission refuses declared File/Workspace payload fields,
  including nested fields, not ordinary JSON keys such as `type: file`.
  Listing runs during admission ignores IDs whose first write is not committed.

- Persist keyed flow runs in the local operator: concurrent starts with the same
  declared key return one run, including after restart or history expiry. Resolve
  flows or machines through the same REST/gRPC key lookup and
  `ava run show --flow <name> --key field=value`; start and summary responses expose
  the permanent key and retained/expired history state.

- Add optional `@ava.workflow(input=..., key=(...))` with the same typed key
  derivation, discovery metadata and existing-key test-host behavior as machines.
  Host run-ID/`--key` lookup and durable key storage remain integration work;
  unkeyed flows retain independent start semantics.
- Run state machines locally through `ava run`: one durable keyed run with
  visit-qualified work, real `after()` timers, event receipts, and paged visit
  reads. `ava machine send` accepts a run ID or typed key fields; REST/gRPC
  expose the same run-owned controls. Generic `ava.start_run` and machine send
  nodes execute on the operator host, with explicit conditional skips and no
  tenant credentials.
- Waiting machine timers survive operator restart. Interrupted visits are not
  replayed automatically: explicitly retry the visit or cancel the run. Each
  new visit imports current code; removed states need attention and existing
  timer deadlines do not move. Machine reruns are refused.

- Keep completed local runs' wait decision cards, audits and replay receipts in
  `state.db`; inspect or replay them after restarting the operator.
- Accept `cron=` and `webhook=` on machine declarations and forward them to the
  normal run carrier.
- Use SQLite `synchronous=FULL` for the local operator's durable state.

- **Breaking for 0.20.x:** remove the built-in machine HTTP client and all
  `DELTA_*` target/credential/retry environment reads, without deprecation.
  Start an instance through the normal run-start path (`ava run <machine>`),
  not `await Machine.start(..., host=...)` or the removed transport kwargs.
  Outside workflows, sends require an explicit `MachineHost` via `host=` and
  an idempotency key; declared sends default to the node's stable step key.
  Replace declared `machine.start` with scheduler-owned `ava.start_run(target, input)`
  for either flows or machines. Both start and send accept `when=`: false skips
  validation and host calls, yields `None`, and satisfies ordering edges without
  skipping children. There is no `deliver` or host-start API.
- Send payloads accept native `File`/`Workspace` models and nested references,
  handed to host admission before serialization; hosts own run-lifetime binding
  before accepting them. Send targets accept either a key tuple or a run ID.
- Key derivation validates only key fields; an existing key ignores replacement
  non-key input/files. General machine reruns are unsupported; replay requires a
  new key, while interrupted repair creates a new visit in the same run. Run
  reads distinguish `expired` terminal evidence from permanent key identity.
- Forward machine cron/webhook declarations to the normal run carrier and
  qualify ordinary and mapped visit contexts consistently.
- **Breaking for 0.20.x:** remove `key_format` from `@ava.machine` and
  `MachineSpec`, including discovery JSON. Keys come from the declared input
  fields via `Machine.key(input)`; remove the decorator argument and any
  formatted-key shorthand. CLI addressing uses run IDs or typed input fields.
- Add an in-memory machine host for workflow `.test()` / `.test_async()`, share
  transition/inbox selection with machine tests, and reject machine steps in
  the embedded runner until a machine-capable operator host is available.
- Block hosted API URLs and hosted environment reads in standalone source CI.

- Operator UI: a machine instance is a run, shown on the normal run page. Runs
  are listed in the run timeline with a readable key label (`acme/api #44`). The
  machine graph is a mode of the shared flow canvas (same cards, grid, controls
  and input dot), one arc per trigger, with the current state highlighted.
  Machine arcs route around the state cards: back arcs run in a lane outside
  the ranks they span, self-loops sit off the card edge, and no arc or label
  crosses a card in either orientation. In the vertical layout the input dot's
  `inputs` label sits above the dot (flows and machines); horizontal unchanged. Visits are a section of the run timeline
  and open in the normal graph, side inspector and log pane. Send, retry visit
  and a development timer trigger sit beside Cancel in the run controls; the
  send form opens in the side panel; pending timers, deferred events, receipts
  and visit waits show in the run waits panel, with the events the current
  state accepts (or "No pending waits"). Instances start from the normal
  run input form (including files and workspaces) and close with the normal run
  cancel. New optional `OperatorApi` methods: machine reads, event send,
  interrupted-visit retry and the development timer trigger; there is no
  machine start, list, close or rerun method or control, and no `key_format`.
  The send form is the run input form over the event payload schema, so `File`
  and `Workspace` payload fields pick and upload like run inputs and reach
  `sendMachineEvent` as `inputFiles`. Keyed flow runs show their key as the run
  label like machine runs. Runs whose history expired under their retention
  policy (`RunSummaryMsg.expired`, visit disposition `expired`) read `expired`
  in the timeline and on the run page and keep their label. The local
  operator's gRPC-web client implements the machine reads and controls over
  `ControlMachine` (instance, history, visit with its scoped snapshot, visit
  facts, send, retry), maps `machine_spec_json`, run keys (labels) and expired
  history, filters wait cards to a visit's qualified steps and maps retained
  decision audits. Event payload files and the development timer trigger are
  not offered over this transport (the host has no attachment field or
  capability for them).
  Run waits (flow decision cards and machine timers, deferred events and
  receipts) are a section of the run timeline instead of a card over the graph;
  the machine graph is fitted clear of the floating timeline and re-fits when
  the canvas height changes, so no card or label sits under the timeline or
  outside the canvas. The "Viewing a run snapshot" banner shows only on settled
  runs, not on live ones. Parallel
  arcs between the same two states fan out, each level through its own label,
  so labels never overlap each other, other arcs or cards. Visit rows wrap full
  state names instead of truncating them.
- **operator-ui run page, host-neutral:** `OperatorApi.liveRunStatuses`
  (optional) declares which of a host's run statuses are live. A live run shows
  no snapshot banner and offers Cancel; every other status is settled. It
  defaults to the local operator's statuses (`requesting`, `pending`,
  `running`, `waiting`, `interrupted`, `needs_attention`), so hosts with their
  own vocabulary (queued or admitted runs) no longer read as settled.
  `runStatusLive` and `LOCAL_LIVE_RUN_STATUSES` are exported. The floating
  timeline's height limit and the graph's fit reserve now attach whenever the
  timeline mounts, including after a host's `loadingFallback`, so the timeline
  no longer covers state cards or the canvas controls there. Flow-run graphs
  get the same reserve as machine graphs, so the floating timeline no longer
  covers flow step cards on narrow windows. Long receipt IDs
  wrap in the send panel and the run waits instead of widening the side panel.
  Machine reads (instance, history, current visit, its event facts) re-read on
  each run revision from the update stream instead of polling every second;
  while the run is live they also poll with backoff (1 s doubling to 30 s), and
  a settled run never polls. A machine read runs one at a time: a revision
  that arrives during a read never aborts it, and starts one follow-up read
  250 ms after it ends, so bursts of revisions faster than the host answers
  still complete reads and end on the latest state (only a new run or visit
  aborts a read). `useMachineInstance` and `useMachineHistory` take
  an optional `{ live, refreshKey }` (`MachineReadRefresh`); without it they
  read once.
- **operator-ui Send event offers accepted and deferred events only:** the
  machine run's Send event menu lists the events the run's current state accepts,
  then the events it defers (its `defer=` list), labelled `(deferred)`; hosts
  buffer a deferred event and acknowledge it as accepted. Events the state
  neither accepts nor defers, which hosts ignore, are no longer offered. The
  choice revalidates on every read: after a transition, a choice the new state
  neither accepts nor defers resets to the first offered event, so a second click
  can no longer send a stale event. A run that can take nothing offers no event
  and Send is disabled; while the run is still loading, the panel says so.
- **operator-ui settled runs show their outcome:** a settled run's controls
  show `Terminal: <outcome>` from the host's terminal seal or, when the host
  reports none, from the run summary's status when it is one of the host's
  terminal outcomes, for flow and machine runs alike. New optional
  `OperatorApi.terminalRunStatuses` declares a host's outcomes; it defaults to
  the local operator's `success`, `failed` and `cancelled`
  (`LOCAL_TERMINAL_RUN_STATUSES`, `runStatusTerminal`). A live run, a run whose
  status is not loaded yet, or a sentinel status such as `unknown` shows no
  outcome. Before, a host without seals (or a machine run) showed an empty
  controls box on the canvas; now no empty box is drawn when there is nothing
  to offer or report.
- **operator-ui machine waits without deferred events:** when a host omits the
  optional deferred events (`pending_events`) on a machine run, a live run's
  Waits say "Deferred events unavailable" instead of developer text, and a
  settled run shows no deferred-events, accepted-events or timer line. While the
  run status is not loaded yet, Waits claim neither "No pending waits." nor
  "Deferred events unavailable".

## 0.20.4

- Remote machine start/send now retry HTTP 429 and transient 5xx or connection
  failures with unchanged instance/event keys. Native async backoff honors
  Retry-After within a 60-second default budget; configure it with
  DELTA_HTTP_RETRY_BUDGET_S or retry_budget_s (zero disables). Retry notices use
  standard INFO logging, and final errors retain status, reason and Retry-After.

## 0.20.3

- No SDK change since 0.20.2; versions move together.

## 0.20.2

- No SDK change since 0.20.1; versions move together.

## 0.20.1

- First published state-machines release; 0.20.0 was tagged but never
  published. No SDK change since 0.20.0.

## 0.20.0 (tagged, never published)

- Add declarative `@ava.machine` state graphs, typed `Event[T]` emissions and
  workflow outputs, discovery manifests, and a native-async local test driver.
  Rename the decision base from `Outcome` to `Event` without an alias;
  `OutcomeUnknownError` keeps its uncertain-effect meaning and name.

- Keep the terminal UI responsive when a streamed update arrives while a resource
  panel is mounting; load the panel only once its widgets are ready.
- Keep graph connections visible across run-topology changes by measuring handle
  bounds after controlled node updates, even when replacement cards are equal-sized.

## 0.19.5

- `decision.map(show=fn)` captures `show` at graph construction as an ordinary
  mapped step `<decision>_show` over the same items, so on deployed runs
  decision child `i` displays item `i`'s show output. Duplicate invocation IDs in
  one graph are refused.

## 0.19.4

- Allow workspace hosts to provide their route-shaped loading surface while the
  initial catalog or selected run is loading, instead of replacing the page with a
  reconnect screen. Hosts can reserve an empty run pane to keep route geometry stable.
- Keep deployment definitions selected when new run summaries arrive on Deployments.

## 0.19.1

- Keep graph connections visible when switching between runs with different steps.
- Operator UI tests: settle the initial log load before simulating reader scrolling,
  avoiding an intermittent scroll-position failure on busy CI runners. Browser
  behavior is unchanged.
- Show graph steps at their final detail level from the first frame, instead of
  briefly showing full cards that then collapse when the graph is zoomed out.

## 0.19.0

- BREAKING-PROTOCOL: 3.0 adds invocation retry/uncertainty declarations, terminal
  and unknown-outcome worker flags, and deployed-invocation step keys. Previous
  protocol-major deployments must be redeployed.

- Workflows: `step.map(...)` and inline `ava.agent.step.map(...)` create runtime
  fan-out, with strict positional zip, broadcast keywords and ordered list results.
  Chained maps pipeline per item. Empty lists create no children. Maps default to
  8 items (`max_items=` can raise this to 64), fail on overflow without truncating,
  and support fail-fast or collect-all item errors. Local fail-fast stops admission
  but cannot interrupt already-running Python calls. Agent children each get a
  fresh agent and keep the existing per-node traces and evaluations.
- Operator, browser and TUI: maps appear as one group with actual-count item strips,
  UI-only placeholders for uncreated chained items, and per-item drill-down. Protocol
  3.0 carries map declarations, run-scoped expansions and item parent/index metadata;
  expansion publication precedes child events.
- Steps accept bounded `retries` and an explicit `on_uncertain` policy, with
  `NonRetryableError`, `OutcomeUnknownError` and a stable `RunContext.step_key`.
  Add typed graph decisions, event waits and sleep, including local buffering,
  retained acceptance receipts, reconciliation, controlled deadlines and CLI
  event/decision submission. `ava.decision.map` provides bounded independent item
  decisions, with optional per-item `show=fn` steps for typed review cards and
  shared context; hosts journal their outputs like ordinary steps. Quorum and
  approve-all are compositions, not bulk resolution.
  Wait nodes park without occupying a worker. Local
  state remains process-local; durable hosts must enforce crash recovery and
  authorization. Nullable timeouts and execution-service receipts propagate
  through waits. Bound reviewed previews and control responses, page wait lists,
  and retain terminal timeout audits and withdrawn cards in operator memory.
  See [Retries and waits](docs/retries-and-waits.md).

## 0.18.0

- Toggle graph node descriptions between two-line previews and fully expanded
  text with the information button beside Fit View. Descriptions appear from
  65% zoom, and graph spacing adapts to taller cards.

- Avalanche and Delta now share one version, starting with 0.18.0. Packages are
  published from the Delta repository; this repository receives the public SDK
  source and release tags as a read-only mirror. Python packages remain on PyPI
  and the operator UI remains on GitHub Packages.

- The Avalanche skill now requires clear, succinct step descriptions that read
  as a coherent story from input to outcome and stay current as steps change.

- Breaking (0.18.0): removed the Ray executor, the `ray` extra, the `--ray`
  operator/dev flags, and the `RayExecutor` export. Operator construction no
  longer accepts `executor_factory`, `executor_backend`, `ray_runtime_env`, or
  `ray_init_kwargs`. The local executor's behavior is unchanged; it is now always
  the default. Delta's separate remote runtime is unchanged.

- `ava init`: new workspaces no longer set up a model provider. There is no
  `scripts/configure-provider.sh`, provider prompt, `.env` file or `codex-lm` extra.
  The starter is now `start_flow`: a source, a plain step, an agent step and a dest,
  each with card text that names its node type. It uses `openai/gpt-5.5` by default, so
  running it as is needs `OPENAI_API_KEY`; to use another provider, change `lm` and
  `sub_lm` in `src/start_flow/flow.py` and set that provider's key. A
  new `README.md` in the workspace explains how to run it and which models and
  credentials PredictRLM, ClaudeAgent and OpenAIAgent take.
- Agents: each agent call now records everything the model is given before its
  first turn, as a new common live event, `prompt.sent`: the system prompt, the
  messages, every tool with its input schema, and the answer's schema. ClaudeAgent
  reads it from the Claude CLI's session transcript, so it includes Claude Code's own
  system prompt and the context the CLI adds to the first message. OpenAIAgent reads
  the system prompt and input from the SDK and the tools and answer format from the
  first response. PredictRLM does not record it yet. The operator API carries the new
  `AGENT_EVENT_KIND_V2_PROMPT_SENT` event kind.
- Web UI: an agent step's **Trace** tab has a **View initial prompt** button. It
  opens a dialog with the system prompt, messages, tools and output schema in
  separate, scrollable sections, each with a copy button; nothing loads until it is
  opened.
- Agent steps: an agent step may call its agent only once per execution. A second
  call, whether sequential, in a loop, concurrent (`asyncio.gather`) or from another
  thread, now raises the new `ava.agent.AgentStepCallError` naming the step, before
  any model work, and fails the step in embedded and operator runs. A failed call
  still counts as the one call; a rerun is a new execution. A step that returns
  without calling its agent logs a warning. Native evaluations now evaluate the
  step's one successful call. Steps that called their agent several times must be
  split into separate agent steps.
- Operator: runs, their details, results and schedules now survive an operator
  restart, including a crash or `kill -9`. `ava operator` and `ava dev` keep their
  state in a local SQLite database, `.avalanche/operator/state.db` under the folder
  they start in by default; pass `--state-db PATH` to choose another file, or
  `--state-db :memory:` for a throwaway one. See "Where the operator keeps its
  data" in the README.
  - **What is kept:** each run with its step states and revisions and what it was
    started with (input, context and the input files it used); its logs, agent
    events and finished traces, classifier invocation snapshots and evaluation
    records; the result index, so `ava result RUN_ID` and `GetRunResult` keep
    working; and each cron schedule's on/off state, input and last started minute,
    kept by workflow, so they also survive a changed cron expression and a workflow
    that leaves discovery and comes back.
    The Web UI, TUI and API show the same history after a restart, and an open
    browser tab or TUI reconnects to the restarted operator on its own (the Web UI
    used to keep retrying with "Response does not belong to the connected operator
    scope" until the page was reloaded). Update sequence numbers keep increasing
    across restarts; each start still gets a new operator instance ID.
  - **What is not:** a run unfinished when the operator stopped is not resumed. On
    the next start the operator stops its leftover run processes, then marks the
    run and its unfinished steps failed with "Operator restarted before the run
    finished; it was not resumed". Evaluations still pending are marked failed the
    same way. `--schedules-enabled` still turns every schedule on, for that start
    only; it is not saved.
  - **Where files go:** result bundles in `results/`, uploaded input files in
    `input-files/` (files a run used are kept, unused uploads are removed) and
    copies of files a schedule's input uses in `schedule-inputs/`, all next to the
    database. Schedule input can refer only to files attached for that schedule
    (or kept from its previous input); other `ava.File` references are refused.
    The database and folders are local and unencrypted and hold step inputs, tool
    outputs and results as recorded.
  - **Nothing expires anymore:** results no longer expire after 24 hours, the
    1024-result and 256 MiB caps are gone, and classifier invocation bodies are no
    longer dropped. Per-item size limits still apply. Nothing is deleted
    automatically.
  - **New `ava state` commands** inspect and clear it, with the operator stopped:
    `ava state info` (database, runs by status, sizes of stored results and input
    files, leftover files), `ava state delete RUN_ID...` (finished runs, with
    everything that belongs to them), `ava state prune --before DATE` or
    `--keep-last N` (old finished runs; never unfinished runs or schedules) and
    `ava state clear` (every run and leftover file, after a confirmation that
    `--yes` skips; schedules only with `--include-schedules`). Large deletions
    reclaim disk space with `VACUUM`.
  - **Startup checks:** only one operator, or one `ava state` command, can use a
    database at a time; another fails with an error naming the file. A database
    written by a newer Avalanche version is refused, and so is a stored schedule
    whose input file is missing (`ava state clear --include-schedules` removes
    it). A result bundle that does not match the index is logged as a warning and
    never served.
  - **Breaking for embedding hosts:** `Operator(...)` now requires a keyword-only
    `state_db` argument (a path or `":memory:"`), and its
    `result_storage_directory` and `result_retention_seconds` arguments are
    removed.

## 0.17.3

- The Web UI's input panel has a file picker for `ava.File` and `list[ava.File]`
  fields. A picked file uploads right away through the new `StageInputFile` gRPC call,
  and Run and the schedule switch send it as an `input_files` attachment;
  `SetScheduleEnabled` now takes `input_files` too. The local operator accepts
  `object_key` attachments only when it staged them itself, and builds each run's
  `ava.File` with `File.from_path`; inline attachment bodies are staged the same way
  and now need a file name. Embedding hosts can support file fields by implementing
  the optional `OperatorApi.stageInputFile`; without it, file fields are not editable.

## 0.17.2

- Operator: importing a light `runtime.operator` submodule, such as
  `runtime.operator.step_io`, no longer loads the whole Operator (its gRPC server,
  process pool and scheduler). The package's public names (`Operator`,
  `WorkflowRegistry`, ...) now load on first use. A host that only builds step
  input/output previews, such as a remote step worker, starts each step about
  0.06 s faster on a laptop and about 0.4 s faster on a 2 vCPU worker.
  `from runtime.operator import Operator` and the `ava` CLI behave as before.

## 0.17.1

- Agents: Avalanche now requires PredictRLM 0.13.2 or later (still below 0.14).
  With `DirectPythonBackend`, the agent's tool calls, such as several `predict()`
  calls in one `asyncio.gather`, now run at the same time instead of one after
  another; a burst of small tool calls no longer hangs until the request
  deadline; each `predict()` makes one model call instead of two; and pending
  async tools are cancelled when an iteration times out or the step is
  cancelled. Its sandboxes also no longer need `mkdir`, `env`, `ps` or `pkill`
  to be installed.

## 0.17.0

- Operator web UI: in a run, a standard Python step's sidebar now opens on **Run
  I/O**, showing what the step received (each argument by parameter name) and what
  it returned in that run. The values are bounded previews built where the step ran:
  plain data as JSON with at most 20 items per collection, 6 levels, 512 characters
  per string and about 8,000 characters in total, other objects (such as data
  frames) as their bounded `repr()`, and anything cut marked with `…` and labeled
  **Truncated preview**. Ray results are never fetched to show them. The operator
  carries them on each node in its gRPC API (`NodeSnapshotV2.step_io`).
- Operator web UI: run sidebars offer **View current definition**, which opens the
  same step's current definition, where its code is shown. Run views still hide code,
  since it reflects the current deployment, not what ran.
- Operator web UI: **Code** is now the first tab of a standard or classifier step's
  definition, and every node type ends with a tab named **Step interface** (formerly
  **Definition** for standard steps and **Step definition** for agents) that no longer
  repeats that title in a nested box. Agent run views gain the same tab.
- **BREAKING CHANGE:** discovered cron schedules now start **disabled**; the operator
  no longer runs a `cron` workflow until its schedule is enabled. Start with
  `ava operator --schedules-enabled` (or `ava dev --schedules-enabled`) to keep the
  previous always-on behavior.
- Enable and disable a workflow's cron schedule from the Web UI, the TUI schedules
  panel, the REST API (`POST /api/v1/flows/{selector}/schedule`), gRPC
  (`SetScheduleEnabled`), and the new `ava schedule list|enable|disable` command.
  Flows report `schedule_enabled`; a disabled schedule has no next run, and an
  enabled one first runs at the next cron occurrence after it was enabled. The state is
  in operator memory only: it is kept across rediscovery and reload and resets on
  restart.
- Enabling a schedule can set the input its cron runs start with. In the Web UI, the
  input panel is the one place to define input: it holds the schedule's input and
  what you type, shows each default as a placeholder, and sends only the values set,
  so the run records defaults as defaults. Run uses it, and turning the schedule on
  saves it for cron runs. The TUI opens its input form prefilled with defaults; the
  REST route and `SetScheduleEnabled` take `input_json`, and `ava schedule enable`
  takes `--input`. Flows report the stored input as `schedule_input_json`; without
  one, scheduled runs use the input defaults.
- Turning a schedule on, or starting a run from the UI, the API, `ava run` or REST,
  is refused with `INVALID_ARGUMENT` when a required input field is missing or null,
  instead of starting a run that fails while it is prepared. The Web UI's error then
  offers an Edit inputs link to the input panel.
- The Web UI marks scheduled workflows with a clock badge. Its run controls gain a
  schedule switch next to Run with a countdown to the next run, and a Schedule
  toggle that shows the cron in plain words with the expression in parentheses. The
  operator republishes the workflow list after each scheduled start, so the next run
  time stays current. The Add JSON input editor is gone; the input panel replaces it.
- **BREAKING CHANGE:** run summaries' `triggered_by` is now one of `scheduled`,
  `manual`, `webhook`, or `api`. REST, gRPC, and `ava run` starts are now `api`
  (they were `manual`); Web UI and TUI starts stay `manual`, and `StartRun` accepts a
  `trigger` field. Clients reject unknown trigger values. Python `RunState` and
  `RunSummary` expose it as `TriggerKind`.
- The browser workflow graph shows a workflow's run input as a black "inputs" dot in
  front of the workflow's first steps, linked to them by dashed lines with a diamond
  head, so it doesn't read as a step. Workflows without `input=` show no dot. It
  grows on hover, and clicking it opens the input panel: in the current definition,
  a box per field to set the value Run and the schedule use, with each field's
  type, default or required status, description and the full schema; in a run, only
  the values the run used, with defaults shown fainter. Hovering the dot names the
  input model and, for a cron workflow, whether scheduled runs use the defaults or
  the schedule's stored input, or that the schedule is off; in a run, it says how
  the run started (manually, by its cron schedule, by a webhook, or through the API).
- The browser workflow graph keeps steps that run one after another on one straight
  row instead of a staircase. Connections from one step that skip ahead past several
  steps now share one line beside the row and branch off to each target, instead of
  each running its own parallel line. Skip connections into the same step from
  different points of a chain merge into one line. The graph keeps separate lines
  when sharing one would make connections cross. Branches are routed
  symmetrically: mirrored branches of a split or a join turn at the same point, and a
  split and its matching join mirror each other. Steps after a join line up with the
  steps before its split, centered between the branches.
- The operator now validates a declared run input once, while it prepares the run,
  and publishes the validated values on the run snapshot. A run with invalid input
  now fails while it is being prepared, before any step is shown, rather than when
  execution starts.
  Input values larger than 1 MiB once serialized, or that cannot be serialized, still
  run, but the UI says their values are unavailable. Fields excluded from serialization, such as
  `Field(exclude=True)` secrets, are shown as not recorded.
- Embedding hosts: `RunSummaryMsg.triggeredBy` is now the `TriggerKind` type
  (`"scheduled" | "manual" | "webhook" | "api"`), and the browser client rejects a run
  summary with any other trigger.

## 0.16.2

- Agents: new runtime `ava.runtimes.OpenAIAgent` runs agent steps with OpenAI models
  through the OpenAI Agents SDK; install it with `avalanche-ai[openai]` and set
  `OPENAI_API_KEY`. The model calls the step's tools, gets `ava.File` inputs as
  attachments (images as images, other files as files it reads), and reads the
  step's Agent Skills through two tools, `load_skill` and `read_skill_file`.
  `builtin_tools=["filesystem", "shell"]` (off by default) turns on the SDK's
  sandbox tools (`apply_patch`, `view_image`, `exec_command`, `write_stdin`): the
  step then runs in a local SDK sandbox rooted at its `ava.Workspace` input, which
  the agent edits in place, or else at a temporary folder; it can return the
  workspace as an `ava.Workspace` output, writes `ava.File` outputs itself, and
  with a shell runs skill scripts. The local sandbox does not confine commands on
  Linux. Without sandbox tools, `ava.File` outputs are text files the model
  answers with and Avalanche writes to the run workspace, and `ava.Workspace`
  fields are rejected. Other settings are `model`, `max_turns`,
  `system_prompt_append`, `narrate` (on by default, as for ClaudeAgent) and
  `tracing` (off: the SDK's own traces are not sent to OpenAI); any other keyword
  is an `agents.ModelSettings` field. Its
  traces have their own format, `openai_agents.responses/v1`, with each response's
  ID, model, reasoning tokens and refusal, and the TUI and browser UI show them,
  including the run's turn limit. A failed run, such as one over `max_turns`, still
  sends its turns and a failed trace header with the reason. The repository's lock
  file now resolves the `openai` package to 3.x (it was 2.x), which the OpenAI
  Agents SDK requires; DSPy and PredictRLM accept it.

## 0.16.1

- `ava.runtimes.ClaudeAgent` takes `system_prompt_preset="claude_code"` to start
  a step's system prompt with Claude Code's own and add the signature's
  instructions, the narration paragraph and `system_prompt_append` after it. The
  answer format, working folder and settings isolation are unchanged. The setting
  is published with the step's runtime and can be set as a workflow default.
  Passing `system_prompt` is still rejected, and the error now names this field.

## 0.16.0

- Long browser log entries keep their time, source and level pinned to the log
  viewport while the message scrolls; these cells leave when their row ends.

- Workflow graph cards use the timeline’s animated status glyphs before monospace
  titles, flat state-tinted 1px borders/backgrounds and 3px corners. Selected cards
  keep a primary outline, including running steps; runtime designation labels remain.

- The Deployments tab marks the current deployment in its right-aligned Status
  column and keeps the pinned Current row on Overview only. Non-current deployments
  have visible primary-colour link-style Set current buttons: no border, background,
  padding or underline, with confirmation, busy feedback and keyboard focus rings.
  Overview run statuses and their header are right-aligned too; dates stay left.

- The browser timeline header now shows Overview / Deployments as keyboard-navigable
  tabs with a primary-color selection underline, or a non-clickable Overview label
  without deployments. The public `"runs"` view value is unchanged. The expand action
  is icon-only; the Timeline heading is gone.

- The browser workspace's pinned row now says "Current definition" without a caret.
  Embedding hosts can optionally override that label, add an independent text action,
  and supply an Overview | Deployments timeline with controlled or local tab state,
  virtualized deployment paging, selection, and confirmed set-current actions.

- Agents: `ava.runtimes.ClaudeAgent` now passes any other keyword to the Claude
  Agent SDK as a `ClaudeAgentOptions` field, such as
  `ClaudeAgent(model="claude-sonnet-5-5", effort="low", mcp_servers={...})`.
  Your MCP servers, hooks, plugins, allowed tools, extra folders and tool or skill
  lists are added to what Avalanche sets for the step; `system_prompt`,
  `output_format` and `cwd` are rejected, and an unknown option name raises. Any
  other option replaces Avalanche's value. These options are never published or
  recorded.
- The browser UI shows which agent steps run on the Claude Agent SDK. Their workflow
  graph nodes have a Claude-orange border, an orange "Agent" label and a Claude logo
  on the corner, and the step sidebar header shows a "Claude Agent SDK" tag. Other
  agent steps keep the purple label. The colored bar on the left of agent and
  classifier nodes is gone.
- The run header in the browser UI's step sidebar is now drawn by each runtime's
  trace viewer. A Claude run shows its model, usage and turn count without
  PredictRLM's "Main" and "Sub" labels; PredictRLM runs look as before.
- A run's topology now records each agent node's runtime (`agent_runtime_kinds`,
  for example `"claude_agent"` or `"predict_rlm"`, with workflow defaults applied),
  so past runs show the right runtime even if the workflow changed. Runs recorded
  before this change show no runtime.
- ClaudeAgent traces say what Claude did. By default the system prompt now asks
  Claude to write a short note before each tool call, and Claude's answer must
  include a `run_summary` of what it did, shown as the answering turn's message and
  removed from the step's outputs. A signature output named `run_summary` now
  raises `AgentStepError` unless `ava.runtimes.ClaudeAgent(narrate=False)`, which
  leaves out both. In the browser UI, a turn that only hands back the step's answer
  without text says so instead of "The model wrote no text this turn".
- ClaudeAgent turns record each response's real token usage and stop reason. Before,
  a turn's output tokens were the count from when the response started, often a
  fraction of the true figure, and `stop_reason` was always empty. A turn for an API
  error the Claude CLI reports itself now has no usage (`null`) rather than a partial
  count.
- Input token counts in the browser UI and TUI include prompt-cache reads and writes,
  which hold most of a Claude session's input. A cached Claude turn showed as "2 in".
- Upgrade dependencies with known security advisories: `filelock` now requires
  `>=4.2.1,<5`; the lockfile moves `msgpack`, `virtualenv`, `graphql-core`,
  `setuptools` and OpenTelemetry to patched releases; the browser UI moves to
  `zod` 4.6.7 and `vitest` 4.1.12.
- Draw clearer connections in the browser UI workflow graph. Each connection now has
  its own start point, end point and path, so connections from different steps no
  longer share lines. Connections that skip a step pass between the cards instead of
  running underneath them along the bottom of the graph, and steps are ordered to
  avoid crossings.
- Fix an intermittent CI failure in the live operator TUI test, which checked for a
  worker's logging output before it had arrived.
- Show long node, run-list, and inspector durations in minutes and hours, with total seconds on hover.
  Agent-event logs use readable durations while retaining exact milliseconds.
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
  @ava.agent_step(Review, runtime=ava.runtimes.PredictRLM(lm=MODEL, skills=[ava.runtimes.predict_rlm.skills.docx]))
  async def review(document: str, *, agent: ava.Agent) -> Report:
      return (await agent(document=document)).outputs.report
  ```

  PredictRLM's skills moved: `ava.agent.Skill` and `ava.agent.skills.pdf`, `.docx`
  and `.spreadsheet` are now `ava.runtimes.predict_rlm.skills.Skill`, `.pdf`, `.docx`
  and `.spreadsheet`. `ava.agent.Skill` and `ava.agent.skills` now mean the new Agent
  Skills type below.

  The entries below give the details. The operator, client and TUI no longer import
  PredictRLM when they start; only the PredictRLM runtime, the `DirectPythonBackend`
  re-export and the TUI's mock trace import it, on first use.
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
  tool-call counts. Details only one runtime has, such as
  PredictRLM's sub-model, iteration limit and per-model usage, travel in the
  summary's runtime part (`TraceHeaderV2.native_json`, typed JSON checked against the
  generated agent schemas on arrival), so the web UI and TUI show them without
  reading the trace body. The protobuf messages change, so the operator, its clients
  and `@trampoline-ai/operator-ui` must be upgraded together. BREAKING-PROTOCOL: part
  of Delta protocol 2.0 (trace header and agent activity fields replaced).
- **Breaking** — Agents: agent steps now take a `runtime=` instead of PredictRLM
  arguments. `lm=`, `sub_lm=`, `max_iterations=`, PredictRLM's own skills and any
  other PredictRLM keyword move into `ava.runtimes.PredictRLM(...)`; passing them to
  `ava.agent.step(...)` or `@ava.agent_step(...)` raises a `TypeError` naming the
  replacement. A step's own `skills=` now takes only `ava.agent.Skill` (see below).
  Workflow defaults move the same way:

  ```python
  # before
  @ava.workflow(agent_defaults={"lm": MODEL, "max_iterations": 10})
  @ava.agent_step(Review, sub_lm=SUB_MODEL, skills=[ava.agent.skills.docx])

  # after
  @ava.workflow(agent_defaults={"runtime": ava.runtimes.PredictRLM(lm=MODEL, max_iterations=10)})
  @ava.agent_step(Review, runtime=ava.runtimes.PredictRLM(sub_lm=SUB_MODEL, skills=[ava.runtimes.predict_rlm.skills.docx]))
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
- **Breaking** — Evaluations: `ctx.inputs` is now the validated inputs of the agent
  call, an instance of the signature's input model as the runtime received it, like
  `ctx.output`. Selectors read fields as attributes:

  ```python
  # before
  state=lambda ctx: {"document": ctx.inputs["document"], "summary": ctx.output.summary}

  # after
  state=lambda ctx: {"document": ctx.inputs.document, "summary": ctx.output.summary}
  ```

  The input paths an evaluation declaration records change the same way (`document`
  instead of `['document']`); the UI shows both as `input.document`.
- **Breaking** — Operator: agent live events and final traces have a new,
  runtime-neutral body (schema version 2). Each live event carries Avalanche's own
  sequence number, the common event (the run start, a finished turn or a tool call)
  when there is one, and the runtime's own details as a typed payload. The final
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
- Agents: a trace's runtime-specific part moves from `AgentTrace.native` into the
  header, `AgentTrace.header.native`, so every shared object has its own runtime
  part. The models are renamed `PredictRLMTraceHeader` (PredictRLM's sub-model,
  iteration limit, per-model usage and telemetry reference) and
  `ClaudeAgentTraceHeader` (the SDK's result subtype, turn count, API time,
  permission denials and errors), both in `avalanche.agent.native`. Code reading
  `ctx.trace.native` in evaluations must read `ctx.trace.header.native`; older
  stored traces are not converted. Because the header travels in every run
  snapshot, its runtime part is limited to 64 KiB; a larger one fails validation,
  and Claude's result errors are kept to the first 8, each cut to 1024 characters
  (a failed result with no error list keeps its result text, cut to 4096 characters). BREAKING-PROTOCOL: part of Delta protocol 2.0
  (final trace body and trace header change; new `TraceHeaderV2.native_json`).
- Agents: new `ava.runtimes.ClaudeAgent` runs agent steps with Claude through the
  Claude Agent SDK. Install the new `claude` extra (`avalanche-ai[claude]`), whose
  SDK package ships the Claude Code CLI, then choose a model with
  `ClaudeAgent(model="claude-haiku-5-5")`.
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
  `Turn.text` is renamed to `Turn.message`, so code reading Claude's reply text from
  `turn.text` must read `turn.message`. PredictRLM's per-iteration `reasoning` field moves into `message`, matching
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
- Agent evidence: the final event's `outputs` describe a PredictRLM step's
  `ava.File` outputs as the file references the step returns (workspace, path, name,
  size and checksum), the same form as its inputs, instead of PredictRLM's own
  `predict_rlm_file` value with the host path. The web UI no longer has a special
  display for `predict_rlm_file` values. BREAKING-PROTOCOL: part of Delta protocol
  2.0 (agent live event bodies change).
- Live agent events: a PredictRLM turn trimmed for size no longer appends an
  "omitted N tool calls" note to the turn's message; the event's omission counts
  (`omitted_tool_calls` and PredictRLM's `omissions`) report it.
- **Breaking** — Agents: the agent evidence contract is simpler. The final event
  (`AgentTraceFinished`) carries the trace `header` and the step's `outputs` instead
  of a `record` with no turns; `AgentTrace` (header and turns) is now only the
  in-process trace in `result.trace` and evaluations, always with its turns, and
  loses `turns_streamed` and `schema_version`. Outputs keep files and workspaces by
  reference, and fields too large to keep (over 4 MiB together) are named in
  `omitted_outputs`; a run that returned none has `outputs: null`. Every call starts
  with a shared `run.started` event holding its inputs (large ones named in
  `omitted_inputs`) and model, replacing PredictRLM's `run.started` and Claude's
  `started` events. A live event now has a common part, a native part or both:
  `native` may be `null`, and events whose runtime part held nothing of its own are
  gone (PredictRLM's `tool.started`, `tool.finished` and `run.succeeded`; Claude's
  `tool.used`, `tool.returned`, `message.finished` and `finished`). PredictRLM's
  `iteration.recorded` part is sent only when a turn's event left calls out. Common
  `tool.started` and `tool.finished` events carry the `turn` that called the tool
  when the runtime knows it (Claude does; PredictRLM does not). `runtime_kind` and
  `trace_format` are no longer stored on trace headers or live events; they are read
  from the runtime part (`header.native.format`). Runtime authors: `emit` takes an
  optional native part, and `finished(header, outputs=..., complete=...)` takes the
  header and the run's raw outputs. The TUI's Output tab and the web UI's Run I/O
  panel read outputs from the final event and inputs from `run.started`, and name the
  fields left out for size. Traces saved by earlier unreleased builds are not
  converted. BREAKING-PROTOCOL: part of Delta protocol 2.0 (live event and final
  trace bodies change; new agent event kind `AGENT_EVENT_KIND_V2_RUN_STARTED`; an
  agent activity's `native_event_kind` is empty for an event without a native part).
- Web UI and TUI: each turn shows its message first and keeps model reasoning in a
  collapsed "Model reasoning" section at the end, shown only when there is some.
  PredictRLM turns show their code and output from the turn's own part and list
  "Predict calls" with their inputs, outputs and details; Claude turns show the
  response's stop reason and API error.
- Agents: agent steps take `skills=`, a list of `ava.agent.Skill` objects in the
  [Agent Skills](https://agentskills.io/specification) format: a name, a
  description saying when to use the skill, Markdown instructions and optional
  resource files. Load an existing `SKILL.md` folder with
  `ava.agent.Skill.from_dir(path)`, which rejects what the format rejects, or build
  one in code. `ava.runtimes.ClaudeAgent` runs them from a temporary plugin made for
  each call; PredictRLM does not support them yet and fails before the agent starts.
  **Breaking:** PredictRLM's own `Skill` and its built-in `pdf`, `docx` and
  `spreadsheet` skills moved to `ava.runtimes.predict_rlm.skills`.
- **Breaking** — Operator: agent declarations gain a `skills` list (name,
  description, instructions and file paths) for the step's Agent Skills. The web UI
  and TUI show them with PredictRLM's own skills; upgrade the operator, its clients
  and `@trampoline-ai/operator-ui` together.
- Docs: new pages for agent runtimes. `docs/agent-runtime-contract.md` shows the data
  every runtime shares (request, result, trace, live events) as typed schemas and
  JSON examples. `docs/runtimes/predict-rlm.md` and `docs/runtimes/claude-agent.md`
  show each runtime's settings, its own trace and event data, and how it fills the
  shared data. `docs/agent-runtime-models.md` lists every model of the layer by
  request, result, trace and live events. The agent API reference links to them, and
  the Avalanche skill carries copies.

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
