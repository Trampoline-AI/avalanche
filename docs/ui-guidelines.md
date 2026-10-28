# UI Guidelines

These rules cover the browser UI under `web/` (the `web/operator` React package and the
`web/local` host that is built into the operator's packaged assets) and the Textual TUI under
`src/tui/`. They are written for reviewers, human or automated, who check a change against how
this repository already builds its interfaces. Each rule has a stable ID so a review comment can
cite it against a specific diff line.

## Shared principles

- `ui/client-only` The browser UI and the TUI are clients. They show operator state and send
  run actions (start, cancel) through the operator API; they never execute nodes, discover
  workflows, or hold live `Workflow` objects. See the "UI state" row in `ARCHITECTURE.md`.
- `ui/trace-viewer-per-format` Presentation that depends on an agent trace format lives in that
  format's viewer: `web/operator/src/agentTrace/viewers/` for the browser and
  `src/tui/agent_trace_viewers/` for the TUI. Components and widgets draw what the viewer
  returns and must not check the trace format themselves (for example, `Inspector.tsx` draws
  `viewer.headerLines(...)` instead of testing for PredictRLM).
- `ui/viewer-registry-complete` Adding a trace format means registering a viewer in both
  registries. In the browser, `TRACE_VIEWERS` uses `satisfies Record<NativeEventFormat, ...>`
  so a format without a viewer fails the build; do not weaken that typing. An unknown format
  raises (`viewerFor` throws) rather than falling back to a generic display.
- `ui/validate-at-boundary` Untyped data from the wire (JSON bodies, `native_json`, user-typed
  run input) is parsed once at the boundary into the typed model and fails loudly on an
  unexpected shape or enum value. Browser examples: `mapTraceHeader` in
  `web/operator/src/api.ts` and `parseRunInput` in `RunControls.tsx`; TUI example:
  `AgentTraceEnvelope.model_validate_json` in `src/tui/ui_store.py`. Components do not probe
  shapes or substitute defaults for missing fields.
- `ui/bounded-buffers` Live data held by a UI is bounded: queues, live descriptor lists, and
  detail caches have explicit size limits (`MAX_PENDING_ENVELOPES`, `MAX_LIVE_DESCRIPTORS` in
  `web/operator/src/state.ts`; the eight-entry, 8 MiB detail caches; `BACKGROUND_UPDATE_CAPACITY`
  and `bound_run` in the TUI). New streams or caches must declare a limit too.

## Browser UI (`web/`)

- `web/package-vs-host` `web/operator` is an embeddable library (`@trampoline-ai/operator-ui`).
  The embedding host owns the operator connection, navigation, and presentation strings.
  Host-specific code such as reading the operator port from page metadata belongs in
  `web/local` (`web/local/src/main.tsx`, `LocalOperatorShell.tsx`), not in `web/operator`.
- `web/host-supplied-copy` Host-facing text and branding come in through `OperatorUiHost` and
  `OperatorUiPresentation` (`web/operator/src/ui/types.ts`). Do not hard-code project names,
  brand images, or host-specific descriptions inside the package.
- `web/public-exports` Anything a host may use is exported from `web/operator/src/index.ts`.
  Changing an exported prop or type, or the embedding behavior the package README describes,
  requires updating `web/operator/README.md`.
- `web/operator-api-only` Components fetch data only through the `OperatorApi` interface
  (`web/operator/src/api.ts`). Do not call `fetch`, create gRPC clients, or import
  `src/generated/operator.client.ts` from components.
- `web/abort-stale-requests` Requests started from a component pass an `AbortSignal`, abort it
  when the selection or component goes away, and ignore results that arrive after an abort
  (see the `AbortController` handling in `ClassifierInspector.tsx`).
- `web/projection-guards` Operator updates are merged by the reducer in
  `web/operator/src/state.ts`, which checks operator instance IDs and event sequence before
  merging. A reset reloads the catalog and run baseline instead of merging incompatible state.
  Do not merge updates into UI state outside this reducer.
- `web/generated-code` Files under `web/operator/src/generated/` are generated from
  `operator.proto` and the agent schemas by `make web-proto`. Do not edit them by hand; a proto
  or agent schema change must include the regenerated files.
- `web/theme-tokens` Colors and fonts come from the `@theme` tokens in
  `web/operator/src/style.css` (`--color-ink`, `--color-muted`, `--color-line`,
  `--color-danger`, ...), used as Tailwind utilities (`text-muted`, `border-line`,
  `bg-panel`) or `var(--color-...)`. Add a token for a new semantic color rather than putting a
  raw hex value in a component.
- `web/tailwind-first` Components style with Tailwind utility classes in `className`. Use
  `style.css` only for what utilities cannot express (third-party overrides such as React Flow,
  authored Markdown typography, animations), and avoid inline `style={{...}}` except for
  values computed at runtime.
- `web/scoped-css` Package CSS is scoped under `.avalanche-operator-ui` by the
  `postcss-prefix-selector` setup in `web/operator/vite.config.ts`. Hand-written rules in
  `style.css` must stay under that scope and must not style host elements or global selectors.
- `web/aria-roles` Interactive and live regions use the ARIA patterns already in the code:
  `role="status"` for loading and progress notices, `role="alert"` for errors,
  `tablist`/`tab`/`tabpanel` with `aria-selected`, `aria-controls` and `aria-labelledby` for
  tabs, and `role="separator"` with `aria-valuenow`/`aria-valuemin`/`aria-valuemax` for
  resizable dividers (`WorkspaceDivider.tsx`). Icon-only buttons need an accessible name.
- `web/keyboard-parity` Every pointer interaction has a keyboard path. Tabs move with
  ArrowLeft/ArrowRight/Home/End and a roving `tabIndex` (`Inspector.tsx`); dividers resize with
  arrow keys; expanded panels and popups close with Escape and return focus to the control that
  opened them (`RunListPanel.tsx`, `InspectorDefinition.tsx`).
- `web/focus-visible` Focusable controls show a `focus-visible` outline using a theme color
  (`focus-visible:outline-2 focus-visible:outline-acid` or the equivalent rule in
  `style.css`). Do not remove outlines without a replacement.
- `web/reduced-motion` Animations and transitions respect `prefers-reduced-motion: reduce`,
  either with a CSS media query (`style.css`) or `window.matchMedia` in script
  (`WorkflowWorkspace.tsx`).
- `web/explicit-states` Loading, error, missing, and unavailable states are shown explicitly
  (for example, a retry action on a failed request, or a message for a tool without source).
  Do not hide a failure by rendering empty content or falling back to current definition data
  for a historical run.
- `web/lint-format` Browser source passes `make web-lint` (ESLint with typescript-eslint,
  `react-hooks/rules-of-hooks`, zero warnings) and `make web-format-check` (Prettier, print
  width 96). Do not add `eslint-disable` comments or config exceptions beyond the narrow
  existing ones.
- `web/no-any` Handwritten TypeScript does not use `any` or `as unknown as` casts; unknown data
  is typed `unknown` and narrowed with a validator or a guard such as `isUnknownRecord` in
  `web/operator/src/guards.ts`.

## TUI (`src/tui/`)

- `tui/single-state-owner` `UIStore` (`src/tui/ui_store.py`) is the only owner of mutable UI
  state. Widgets and screens read from the store and call store methods; they do not keep their
  own copy of run, selection, or filter state.
- `tui/thread-safety` Background threads and provider callbacks never touch widgets or store
  fields directly. They enqueue work through the store's `enqueue_*` methods (or its background
  update queue), and the UI-thread tick (`AvalancheApp._tick` calling `UIStore.tick`) applies
  it.
- `tui/no-poll-in-tick` The render tick does not read provider data. Provider reads are
  triggered by selection changes, committed stream updates, or explicit invalidation, and run
  off the UI thread (see the comment in `UIStore.tick`).
- `tui/state-provider-boundary` The TUI reaches the operator only through the `StateProvider`
  boundary (`GrpcStateProvider` for a real operator, `MockStateProvider` for `ava tui` without
  `--connect`). Changes that add data to the TUI extend the provider and the mock together.
- `tui/viewers-no-textual` Trace viewers in `src/tui/agent_trace_viewers/` return plain frozen
  data (`TraceSection`, `TraceGroup`, header lines) and import nothing from Textual. The widget
  draws that data and the store walks the same tree for keyboard navigation.
- `tui/theme-constants` Colors and status styles come from `src/tui/theme.py` (the glacier
  palette constants, `STATUS_STYLES`, `status_display`, `AVALANCHE_THEME`). Status presentation
  goes through `status_display` so unknown statuses get the "other" style instead of borrowing
  an enum's meaning.
- `tui/inline-css` Textual styles are declared in `CSS` or `DEFAULT_CSS` strings on the app,
  screen, or widget class that uses them; the TUI has no separate `.tcss` files.
- `tui/bindings` App-wide keys are declared in `AvalancheApp.BINDINGS` with `Binding(...)`,
  using `priority=True` where a key must win over focused widgets. Modal screens bind
  `escape` to `dismiss` (`screens/tools.py`, `screens/input_form.py`).

## Tests

- `test/tui-headless-and-tmux` A TUI change that affects rendering, sizing, or input needs a
  headless Textual test in `test/tui_test.py` (`app.run_test(size=...)` with `pilot.press`)
  and, for real-terminal behavior, a test in `test/tui_tmux_test.py` marked
  `pytest.mark.tmux`. Trace viewer changes are covered in `test/tui_agent_trace_viewers_test.py`.
- `test/web-behavior` Browser changes come with Vitest and Testing Library tests next to the
  component (`*.test.tsx` in `web/operator/src/`) that drive the UI through DOM events
  (`fireEvent`) and check the resulting behavior.
- `test/query-by-role` Tests locate UI by role and accessible name (`getByRole`,
  `findByRole`, `getByLabelText`) and assert state, visibility, and ARIA attributes. Do not
  assert exact user-facing copy unless the wording is the behavior under test, and do not
  duplicate labels or add test-only attributes just to find an element.
- `test/tui-refresh-budget` Changes to TUI refresh paths (log panel, run history, DAG drawing)
  must keep `make tui-bench` within its frame budget (`scripts/benchmark_tui.py`).

## Packaging and change records

- `pkg/web-assets-fresh` Any change under `web/` that affects the built output must include
  the rebuilt `src/runtime/operator/web_assets/` files (`make web-build`). CI runs
  `make web-assets-check` and fails on stale or missing generated assets. Do not hand-edit
  files in `web_assets/`.
- `pkg/changelog` A user-visible UI change adds an entry under `## Unreleased` in
  `CHANGELOG.md`, as recent `feat(web)` and `fix(web)` commits do.
- `pkg/pnpm-workspace` Browser dependencies are managed with pnpm in the `web/` workspace;
  dependency changes update `web/pnpm-lock.yaml` (CI installs with `--frozen-lockfile`).
  `web/operator` keeps `react` and `react-dom` as peer dependencies.
