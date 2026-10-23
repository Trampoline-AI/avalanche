"""AvalancheApp — main TUI application backed by StateProvider."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import grpc
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Header

from runtime.operator.client import OperatorCallError

from .dag_layout import DagNode
from .extensions import ExtensionSession
from .mock import MockStateProvider
from .models import RunState, RunStatus, TraceDetail, WorkflowInfo
from .screens.input_form import InputForm
from .screens.tools import InspectionScreen, LogFilterScreen, ResourceScreen
from .screens.workflow_detail import WorkflowDetailScreen
from .state import (
    ActionDescriptor,
    ActionResult,
    Object,
    PanelDescriptor,
    ReadOnlyStateProvider,
    ScopeDescriptor,
    StateProvider,
    TuiContext,
    TuiExtensionError,
    get_operator_reachability,
    get_tui_extensions,
)
from .theme import AVALANCHE_THEME
from .ui_store import TraceDetailCompletion, TraceHydrationKey, UIStore
from .widgets.agent_trace import (
    AgentMetadataInspector,
    AgentOutputInspector,
    AgentTraceInspector,
)
from .widgets.log_panel import LogWidget
from .widgets.run_history import RunHistoryWidget
from .widgets.sidebar import Sidebar

# Widget IDs for the four selectable panes
_PANE_WIDGET_IDS = {
    "sidebar": "sidebar",
    "dag": "dag-container",
    "run-history": "run-history",
    "log": "log-panel",
    "trace": "agent-trace-inspector",
}

_TRACE_HYDRATION_RETRY_BASE_SECONDS = 0.25
_TRACE_HYDRATION_RETRY_MAX_SECONDS = 4.0
_TRACE_HYDRATION_RETRY_MAX_LEVEL = 5


class AvalancheApp(App):
    """Avalanche TUI — workflow monitor backed by StateProvider."""

    TITLE = "avalanche"
    SUB_TITLE = ""

    CSS = """
    ScrollableContainer {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("?", "context_help", "Help"),
        Binding("f", "log_filters", "Log filters"),
        Binding("i", "inspect_node", "Inspect node"),
        Binding("c", "catalog", "Catalog"),
        Binding("ctrl+s", "schedules", "Schedules"),
        ("escape", "escape_key", "Esc"),
        Binding("tab", "workflow_next", "Tab", priority=True),
        Binding("shift+tab", "workflow_prev", "Shift+Tab", priority=True),
        Binding("r", "start_run", "Run", priority=True),
        Binding("x", "cancel_run", "Stop", priority=True),
        Binding("a", "toggle_run_actions_menu", "Actions", priority=True),
        Binding("left", "nav_left", "←", priority=True),
        Binding("right", "nav_right", "→", priority=True),
        Binding("up", "nav_up", "↑", priority=True),
        Binding("down", "nav_down", "↓", priority=True),
        Binding("alt+up", "run_prev", "Alt+↑", priority=True),
        Binding("alt+down", "run_next", "Alt+↓", priority=True),
        Binding("pageup", "log_page_up", "PgUp", priority=True),
        Binding("pagedown", "log_page_down", "PgDn", priority=True),
        ("s", "toggle_autoscroll", "Autoscroll"),
        ("w", "toggle_wrap", "Wrap"),
        Binding("d", "toggle_dag", "DAG", priority=True),
        Binding("l", "toggle_logs", "Logs", priority=True),
        ("enter", "activate", "Enter"),
        Binding("e", "expand_trace_hierarchy", "Expand all"),
        Binding("z", "collapse_trace_hierarchy", "Collapse all", priority=True),
    ]

    def __init__(
        self,
        provider: StateProvider | None = None,
        workflow: str | None = None,
        node: str | None = None,
        *,
        close_provider_on_unmount: bool = True,
        run: str | None = None,
        read_only: bool = False,
    ):
        super().__init__()
        self.register_theme(AVALANCHE_THEME)
        self.theme = "avalanche"
        defer_initial_catalog = provider is not None
        if provider is None:
            provider = MockStateProvider(include_agent_trace=workflow == "agent_trace")
        if read_only:
            provider = ReadOnlyStateProvider(provider)
        self.store = UIStore(
            provider, defer_initial_catalog=defer_initial_catalog, read_only=read_only
        )
        self.read_only = read_only
        self._deep_link_run = run
        self._deep_link_run_in_flight = False
        self._requested_flow = workflow
        self._close_provider_on_unmount = close_provider_on_unmount
        self._timer: Timer | None = None
        self._screen: WorkflowDetailScreen | None = None
        self._leader_pending: bool = False
        self._log_autoscroll: bool = True
        self._log_wrap: bool = False
        self._trace_hydration_in_flight: set[TraceHydrationKey] = set()
        self._trace_hydration_attempts: dict[TraceHydrationKey, int] = {}
        self._trace_hydration_attempt_counter = 0
        self._trace_hydration_superseded: set[int] = set()
        self._trace_hydration_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="avalanche-trace-hydration",
        )
        self._trace_hydration_closed = False
        self._run_control_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="avalanche-run-control",
        )
        self._cancel_run_requests: set[str] = set()
        self._run_controls_closed = False
        self._dag_visible = True
        self._restore_dag_focus = False
        self._restore_log_focus = False
        self._logs_visible = True
        self._run_actions_menu_open = False
        self._trace_hydration_retry: dict[TraceHydrationKey, tuple[int, float]] = {}
        self._trace_hydration_context: TraceHydrationKey | None = None
        self._deep_link_workflow = workflow
        self._deep_link_node = node
        self._operator_was_reachable: bool | None = None
        self.extension_session: ExtensionSession | None = None
        self._extension_baseline_generation = 0
        self._scope_reload_pending = False
        self._extension_token: tuple | None = None
        self._context_generation = 0
        self._scope: tuple[tuple[str, str], ...] = ()
        self._action_intents: dict[tuple[TuiContext, str], tuple[str, str]] = {}
        self._actions_in_flight: set[tuple[TuiContext, str]] = set()
        self._apply_deep_link()

    def _apply_deep_link(self) -> None:
        """Apply a deep link once its asynchronously loaded catalog is available."""
        workflow = self._deep_link_workflow
        node = self._deep_link_node
        if workflow:
            match = next((p for p in self.store.workflows if p.selector == workflow), None)
            if match is None:
                short_matches = [
                    p
                    for p in self.store.workflows
                    if workflow in {p.name, p.rendered_name, p.builder_symbol}
                ]
                match = short_matches[0] if len(short_matches) == 1 else None
            if match:
                self.store.switch_workflow(match)
                self._deep_link_workflow = None
            else:
                return
        if self._deep_link_run is not None:
            if not self.store.workflows or self._deep_link_run_in_flight:
                return
            run_id = self._deep_link_run
            self._deep_link_run_in_flight = True
            epoch = self.store._workflow_context_epoch

            def load_link() -> None:
                try:
                    run = self.store.provider.get_run(run_id)
                except Exception:
                    run = None
                self.call_later(self._complete_run_link, run_id, epoch, run)

            self._run_control_executor.submit(load_link)
            return
        if node:
            match = next((n for n in self.store.all_nodes if n.name == node), None)
            if match is None:
                match = next((n for n in self.store.all_nodes if n.display_name == node), None)
            if match:
                self.store.select_node(match)
                self._deep_link_node = None

    def _complete_run_link(self, run_id: str, epoch: int, run: RunState | None) -> None:
        self._deep_link_run_in_flight = False
        self._deep_link_run = None
        if epoch != self.store._workflow_context_epoch:
            return
        if run is None:
            self.store.run_error = f"Linked run {run_id} is unavailable"
            return
        matches = [flow for flow in self.store.workflows if self.store._run_matches(run, flow)]
        current = self.store.current_workflow
        if len(matches) != 1 or (
            self._requested_flow is not None
            and current is not None
            and current.selector != matches[0].selector
        ):
            self.store.run_error = "Flow/run deep link does not match the loaded catalog"
            return
        if current is None or current.selector != matches[0].selector:
            self.store.switch_workflow(matches[0])
        self.store.switch_run(run)
        self._apply_deep_link()
        self._refresh_widgets()

    @property
    def extension_context(self) -> TuiContext:
        store = self.store
        flow = store.current_workflow.selector if store.current_workflow else None
        run = store.selected_run_id
        node = store.selected_node.name if store.selected_node else None
        token = (
            self._scope,
            flow,
            run,
            node,
            store._workflow_context_epoch,
            store._run_interaction_generation,
            store._latest_reset_generation,
        )
        if token != self._extension_token:
            self._extension_token = token
            self._context_generation += 1
            self._action_intents.clear()
            self._actions_in_flight.clear()
        return TuiContext(
            self._scope, flow=flow, run=run, node=node, generation=self._context_generation
        )

    def get_system_commands(self, screen):
        yield SystemCommand("Catalog", "Browse workflow definitions", self.action_catalog)
        yield SystemCommand("Run history", "Focus loaded run history", self._focus_history)
        yield SystemCommand(
            "Inspect node",
            "State, timing, errors and trace availability",
            self.action_inspect_node,
        )
        yield SystemCommand(
            "Schedules", "Inspect schedules; no simulated toggle", self.action_schedules
        )
        yield SystemCommand(
            "Log filters", "Node, attempt, channel and level", self.action_log_filters
        )
        yield SystemCommand(
            "Follow / pause logs", "Toggle live log following", self.action_toggle_autoscroll
        )
        yield SystemCommand("Help", "Keys and current context", self.action_context_help)
        if self.extension_session is None:
            if self.can_start_run():
                yield SystemCommand(
                    "Start run", "Start the selected workflow", self.action_start_run
                )
            if self.can_cancel_selected_run():
                yield SystemCommand(
                    "Cancel run", "Request cancellation", self.action_cancel_run
                )
        else:
            description = self.extension_session.description
            if description is not None:
                for panel in (
                    description.panels if "paged-resources" in description.features else ()
                ):
                    yield SystemCommand(
                        f"{panel.slot} / {panel.label}",
                        f"Open {panel.renderer} panel",
                        lambda panel=panel: self.open_extension_panel(panel),
                    )
                for action in description.actions:
                    if (
                        action.input_schema is not None
                        and "json-schema-forms" not in description.features
                    ):
                        continue
                    unavailable = not action.available or (self.read_only and action.mutates)
                    reason = "read-only" if self.read_only and action.mutates else action.reason
                    label = (
                        f"{action.label} · disabled: {reason}" if unavailable else action.label
                    )
                    yield SystemCommand(
                        label,
                        action.permission or "Provider action",
                        lambda action=action: self.invoke_extension_action(action),
                    )
                for scope in description.scopes:
                    yield SystemCommand(
                        f"Scope / {scope.label}",
                        scope.reason or "Switch scope",
                        lambda scope=scope: self.select_extension_scope(scope),
                    )

    def _focus_history(self) -> None:
        self.store.focused_pane = "run-history"
        self._refresh_widgets()

    def action_catalog(self) -> None:
        self.store.sidebar_visible = True
        self.store.focused_pane = "sidebar"
        self._sync_sidebar_focus()
        self._refresh_widgets()

    def action_context_help(self) -> None:
        context = self.extension_context
        self.push_screen(
            InspectionScreen(
                "Contextual help",
                (
                    f"Flow: {context.flow or 'none'}\nRun: {context.run or 'none'}\n"
                    f"Node: {context.node or 'none'}\n"
                    f"Mode: {'read-only' if self.read_only else 'interactive'}\n\n"
                    "Ctrl+P  command palette\nTab / Shift+Tab  workflow\nSpace E / C  catalog\n"
                    "Arrows  pane navigation\nAlt+arrows  select run\n"
                    "I  node/attempt inspection\n"
                    "Enter  agent trace\nEscape  back / clear selection\n"
                    "L / D  logs / DAG\nS  follow / pause logs\nF  filters\n"
                    "/  loaded-window search\n"
                    "N / Shift+N  search matches\nE / Z  expand / collapse\n"
                    "Ctrl+S  schedules\n\n"
                    "Provider actions and optional panels appear only when described.\n"
                    "Missing attempts, timestamps or channels are not exposed, never invented."
                ),
            )
        )

    def action_log_filters(self) -> None:
        self.push_screen(LogFilterScreen())

    def action_schedules(self) -> None:
        lines = ["Schedule control is not exposed by the legacy provider."]
        for flow in self.store.workflows:
            if flow.cron:
                lines.extend(
                    [
                        f"\n{flow.rendered_name}",
                        f"  cron: {flow.cron}",
                        f"  next dispatch timestamp: {flow.next_run_at}",
                        f"  last dispatch timestamp: {flow.last_run_at}",
                    ]
                )
        if len(lines) == 1:
            lines.append("No schedules in the loaded catalog.")
        self.push_screen(InspectionScreen("Schedules", "\n".join(lines)))

    def action_inspect_node(self) -> None:
        import json

        store = self.store
        selected = store.selected_node
        node = (
            store.current_run.nodes.get(selected.name)
            if selected is not None and store.current_run is not None
            else None
        )
        if node is None:
            content = "Select a node with run data. Attempt metadata is not exposed."
        else:
            content = json.dumps(
                {
                    "node": node.node_id,
                    "state": node.status.value,
                    "revision": node.revision,
                    "elapsed_seconds": node.elapsed,
                    "error": node.error,
                    "trace_available": node.trace.available if node.trace else None,
                    "attempts": "not exposed by legacy provider",
                },
                indent=2,
            )
        self.push_screen(InspectionScreen("Node / attempt detail", content))

    def open_extension_panel(self, panel: PanelDescriptor) -> None:
        if self.extension_session is not None:
            self.push_screen(
                ResourceScreen(self.extension_session, panel, self.extension_context)
            )

    def select_extension_scope(self, scope: ScopeDescriptor) -> None:
        if not scope.selectable:
            self.notify(scope.reason or "Scope unavailable", severity="warning")
            return
        self._scope = scope.context.scope
        self._scope_reload_pending = True
        self._context_generation += 1
        self.store._workflow_context_epoch += 1
        self.store._run_interaction_generation += 1
        self.store._cancel_detail_hydration_repairs()
        self.store.close_trace_inspector()
        self.store.current_run = None
        self.store.current_workflow = None
        self.store.workflows = []
        self.store._set_runs_cache([])
        self.store.dag = None
        self.store.all_nodes = []
        self._refresh_widgets()

    def invoke_extension_action(self, action: ActionDescriptor) -> None:
        session = self.extension_session
        if session is None:
            return
        if (self.read_only and action.mutates) or not action.available:
            self.notify(
                "Read-only"
                if self.read_only and action.mutates
                else action.reason or "Action unavailable",
                severity="warning",
            )
            return
        context = self.extension_context
        if (
            action.input_schema is not None
            and "json-schema-forms" not in session.extension.features
        ):
            self.notify("Provider does not expose schema forms", severity="warning")
            return
        if action.input_schema is not None or action.confirmation or action.mutates:
            schema = action.input_schema or {"type": "object", "properties": {}}
            self.push_screen(
                InputForm(
                    schema,
                    context,
                    lambda payload: self._invoke_extension(action, context, payload),
                    title=action.confirmation or action.label,
                )
            )
        else:
            self._invoke_extension(action, context, {})

    def _invoke_extension(
        self, action: ActionDescriptor, context: TuiContext, payload: Object
    ) -> None:
        from uuid import uuid4

        session = self.extension_session
        if session is None or context != self.extension_context or session.context != context:
            return
        key = (context, action.id)
        if key in self._actions_in_flight:
            return
        import json

        from .state import json_plain

        encoded = json.dumps(json_plain(payload), sort_keys=True, separators=(",", ":"))
        previous = self._action_intents.get(key)
        if previous is not None and previous[1] != encoded:
            self.notify(
                "Previous outcome is uncertain; retry the same payload or change context",
                severity="warning",
            )
            return
        intent, _ = self._action_intents.setdefault(key, (str(uuid4()), encoded))

        def complete(result: ActionResult) -> None:
            self._actions_in_flight.discard(key)
            if result.status == "completed":
                self._action_intents.pop(key, None)
            # Accepted is a request acknowledgement, never a terminal run outcome.
            self.notify(f"{action.label}: {result.status}")

        def failed(error: TuiExtensionError) -> None:
            self._actions_in_flight.discard(key)
            self.notify(f"{error.code}: {error}", severity="error")

        if session.invoke(action, payload, intent, complete, failed):
            self._actions_in_flight.add(key)

    def compose(self) -> ComposeResult:
        yield Header()

    def on_mount(self) -> None:
        self._screen = WorkflowDetailScreen()
        self.push_screen(self._screen)

        extension = get_tui_extensions(self.store.provider)
        if extension is not None:
            self.extension_session = ExtensionSession(
                extension,
                read_only=self.read_only,
                post=lambda callback: self.call_later(callback),
            )
        self.store.provider.on_run_update(self._on_run_update_bg)
        self.store.provider.on_catalog_update(self.store.enqueue_catalog_update)
        self.store.provider.on_detail_update(self.store.enqueue_detail_update)
        self.store.provider.on_log(lambda _: None)
        self.store.provider.start_stream()

        self._timer = self.set_interval(1 / 30, self._tick)

    def on_unmount(self) -> None:
        """Cancel provider work and join both hydration workers."""
        # A fatal trace error stops fetching before resource teardown begins.
        if self._run_controls_closed:
            return
        self._trace_hydration_closed = True
        self._run_controls_closed = True
        self.store.request_shutdown()
        if self.extension_session is not None:
            self.extension_session.close(wait=False)
        close = getattr(self.store.provider, "close", None)
        try:
            if self._close_provider_on_unmount and callable(close):
                close()
        finally:
            if self.extension_session is not None:
                self.extension_session.close()
            self.store.shutdown()
            self._trace_hydration_executor.shutdown(wait=True, cancel_futures=True)
            self._run_control_executor.shutdown(wait=True, cancel_futures=True)

    def _on_run_update_bg(self, run: RunState) -> None:
        """Called from background thread when a run state changes."""
        self.store.enqueue_run_update(run)

    # ── Tick ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        catalog_revision = self.store.catalog_revision
        self.store.tick()
        session = self.extension_session
        if session is not None:
            session.select(self.extension_context)
            changed = session.tick()
            if session.awaiting_baseline:
                if (
                    self.store._latest_reset_generation > self._extension_baseline_generation
                    and not self.store._reset_reconciliations_in_flight
                ):
                    session.reconciled()
            elif changed:
                self.store._refresh_workflow_catalog()
                self.store._refresh_runs_cache()
                if isinstance(self.screen, ResourceScreen):
                    if (
                        self.screen.panel.resource in session.changed_resources
                        or "*" in session.changed_resources
                    ):
                        self.screen._revision = None
                        self.screen._load(None)
            if not session.awaiting_baseline:
                self._extension_baseline_generation = self.store._latest_reset_generation
            if self._scope_reload_pending and session.description is not None:
                self._scope_reload_pending = False
                self.store._refresh_workflow_catalog()
        self._apply_trace_hydration_completions()
        self._hydrate_selected_trace()
        if self.store.catalog_revision != catalog_revision:
            self._apply_deep_link()
            if self._screen:
                self._screen._remount_dag()
        if self._deep_link_workflow or self._deep_link_run or self._deep_link_node:
            self._apply_deep_link()
        self.store.auto_follow_latest_run()
        self._refresh_widgets()
        self._autoscroll_logs()
        self._check_connection()

    def _refresh_widgets(self) -> None:
        """Refresh all visible widgets to reflect current store state."""
        self._normalize_focused_pane()
        if not self._screen:
            return
        try:
            dashboard = self._screen.query_one("#dashboard-pane")
            inspector = self._screen.query_one("#agent-trace-inspector")
            trace_content = self._screen.query_one("#agent-trace-content", AgentTraceInspector)
            metadata_content = self._screen.query_one(
                "#agent-metadata-content", AgentMetadataInspector
            )
            output_content = self._screen.query_one(
                "#agent-output-content", AgentOutputInspector
            )
            dashboard.display = not self.store.trace_inspector_open
            inspector.display = self.store.trace_inspector_open
            active_tab = self.store.trace_inspector_tab
            trace_content.display = active_tab == "trace"
            output_content.display = active_tab == "output"
            metadata_content.display = active_tab == "metadata"
            node_id = self.store.selected_agent_node_id
            workflow = self.store.current_workflow
            display_name = (
                workflow.display_names.get(node_id, node_id)
                if workflow is not None and node_id is not None
                else "Agent step"
            )
            inspector.border_title = f"Agent {display_name}"
        except NoMatches:
            pass
        try:
            self._screen.sync_controls()
        except NoMatches:
            pass
        context = self.extension_context
        scope = " / ".join(label for _, label in context.scope)
        crumbs = " › ".join(
            item for item in (scope, context.flow, context.run, context.node) if item
        )
        mode = "READ ONLY" if self.read_only else "interactive"
        try:
            self._screen.query_one("#breadcrumbs").update(
                f"{mode} · {crumbs or 'Catalog'} · Ctrl+P commands · ? help"
            )
        except NoMatches:
            pass
        # Show/hide sidebar + grip, sync width
        visible = self.store.sidebar_visible
        try:
            sidebar = self._screen.query_one("#sidebar", Sidebar)
            sidebar.display = visible
            if visible:
                sidebar.styles.width = self.store.sidebar_width
        except NoMatches:
            pass
        # Toggle -pane-active class on the focused pane (lazygit-style borders)
        focused = self.store.focused_pane
        for pane_name, widget_id in _PANE_WIDGET_IDS.items():
            try:
                w = self._screen.query_one(f"#{widget_id}")
                if pane_name == focused:
                    w.add_class("-pane-active")
                else:
                    w.remove_class("-pane-active")
                w.refresh()
            except NoMatches:
                pass
        # Refresh non-pane widgets
        for widget_id in ("status-bar", "dag-panel"):
            try:
                self._screen.query_one(f"#{widget_id}").refresh()
            except NoMatches:
                pass
        # Run history still sizes itself to its complete table.
        try:
            self._screen.query_one("#run-history-content").refresh(layout=True)
        except NoMatches:
            pass
        if self.store.trace_inspector_open:
            for content_id in (
                "agent-trace-content",
                "agent-output-content",
                "agent-metadata-content",
            ):
                try:
                    self._screen.query_one(f"#{content_id}").refresh(layout=True)
                except NoMatches:
                    pass
        # Update sticky headers
        try:
            from .widgets.run_history import RunHistoryWidget

            self._screen.query_one("#run-history-header").update(
                RunHistoryWidget.render_header()
            )
        except NoMatches:
            pass
        try:
            self._screen.query_one("#log-header").update(LogWidget.render_header())
        except NoMatches:
            pass
        # Update border titles with run context
        run_id = self.store.selected_run_id
        run_suffix = f" · {run_id}" if run_id else ""
        try:
            dag = self._screen.query_one("#dag-container")
            dag.border_title = f"DAG{run_suffix}"
            # Show center button only when DAG is scrollable
            btn = self._screen.query_one("#dag-center-btn")
            btn.display = dag.max_scroll_x > 0 or dag.max_scroll_y > 0
        except NoMatches:
            pass
        try:
            from .widgets.run_history import _cron_description, _next_run_label

            rh = self._screen.query_one("#run-history")
            rh.border_subtitle = (
                f"{self.store.history_evicted:,} older runs outside loaded window"
                if self.store.history_evicted
                else ""
            )
            n_runs = len(self.store.runs_for_current_workflow)
            left = f"{n_runs} Run{'s' if n_runs != 1 else ''}" if n_runs else "Runs"

            pipe = self.store.current_workflow
            right = ""
            if pipe and pipe.cron:
                desc = _cron_description(pipe.cron)
                right = f"Scheduled {desc}"
                t = _next_run_label(pipe.cron, pipe.next_run_at)
                if t:
                    right += f" · next run {t}"

            if right:
                avail = rh.size.width - 6
                pad = max(3, avail - len(left) - len(right))
                line_color = (
                    "#60dce4" if self.store.focused_pane == "run-history" else "#5a4f80"
                )
                line = f"[{line_color}]{'─' * (pad - 2)}[/]"
                rh.border_title = f"{left} {line} {right}"
            else:
                rh.border_title = left
        except NoMatches:
            pass
        # Log panel border title + toggle hints + wrap width
        try:
            from .dag_layout import marker_for
            from .models import NodeStatus

            lp = self._screen.query_one("#log-panel")
            store = self.store

            # Left part: Logs · node · run_id
            run_suffix = f" · {store.selected_run_id}" if store.selected_run_id else ""
            node = store.selected_node
            if node:
                ns = store.node_statuses.get(node.name, NodeStatus.PENDING)
                m = marker_for(node, ns, store.frame)
                left = f"Logs: {m} {node.display_name}{run_suffix}"
            else:
                left = f"Logs{run_suffix}"

            # Toggle hints pushed to the right via line padding
            a = "on" if self._log_autoscroll else "off"
            w = "on" if self._log_wrap else "off"
            right = f"Autoscroll {a} (s) · Wrap {w} (w)"
            # Textual adds 1 space on each side of title text, plus 2 border chars
            avail = lp.size.width - 6
            pad = max(3, avail - len(left) - len(right))
            # Line color matches border: bright when focused, dim when not
            line_color = "#60dce4" if self.store.focused_pane == "log" else "#5a4f80"
            line = f"[{line_color}]{'─' * (pad - 2)}[/]"
            lp.border_title = f"{left} {line} {right}"

            log_w = self._screen.query_one("#log-content", LogWidget)
            # Subtract 2: 1 for the scrollbar gutter + 1 for breathing room
            log_w.wrap_width = (lp.content_size.width - 2) if self._log_wrap else 0
            log_w.sync_from_store()
        except NoMatches:
            pass

    def _sync_sidebar_focus(self) -> None:
        """Keep Textual focus in sync with store.focused_pane."""
        try:
            sidebar = self._screen.query_one("#sidebar", Sidebar)
            if self.store.focused_pane == "sidebar":
                sidebar.focus()
            else:
                sidebar.blur()
        except Exception:
            pass

    # ── Log autoscroll ────────────────────────────────────────────────

    def _autoscroll_logs(self) -> None:
        """Scroll the virtualized log view to its last row."""
        if not self._log_autoscroll:
            return
        try:
            log_view = self._screen.query_one("#log-content", LogWidget)
            target = log_view.max_scroll_y
            if target > 0 and log_view.scroll_y != target:
                log_view.scroll_target_y = target
                log_view.scroll_y = target
        except Exception:
            pass

    def _selected_trace_hydration_key(self) -> TraceHydrationKey | None:
        if not self.store.trace_inspector_open or self.store.trace_inspector_tab != "trace":
            return None
        run = self.store.current_run
        node_id = self.store.selected_agent_node_id
        if run is None or node_id is None:
            return None
        node = run.nodes.get(node_id)
        descriptor = node.trace if node is not None else None
        if descriptor is None or not descriptor.available:
            return None
        return (run.run_id, node_id, descriptor.revision)

    def _sync_trace_hydration_context(self, key: TraceHydrationKey | None) -> None:
        if key == self._trace_hydration_context:
            return
        self._trace_hydration_context = key
        for active_key, attempt in self._trace_hydration_attempts.items():
            if active_key != key:
                self._trace_hydration_superseded.add(attempt)

    def _release_trace_hydration_attempt(self, attempt: int) -> TraceHydrationKey | None:
        active_key = next(
            (
                key
                for key, active in self._trace_hydration_attempts.items()
                if active == attempt
            ),
            None,
        )
        if active_key is not None:
            self._trace_hydration_attempts.pop(active_key)
            self._trace_hydration_in_flight.discard(active_key)
            self._trace_hydration_superseded.discard(attempt)
        return active_key

    def _apply_trace_hydration_completions(self) -> None:
        for completion, applied in self.store.take_trace_hydration_completions():
            active_key = self._release_trace_hydration_attempt(completion.attempt)
            if active_key is None:
                continue
            if applied:
                self._trace_hydration_retry.pop(active_key, None)
                continue
            if not self._trace_detail_still_relevant(completion):
                self._trace_hydration_retry.pop(active_key, None)
                continue
            level = min(
                self._trace_hydration_retry.get(active_key, (0, 0.0))[0] + 1,
                _TRACE_HYDRATION_RETRY_MAX_LEVEL,
            )
            delay = min(
                _TRACE_HYDRATION_RETRY_BASE_SECONDS * (2 ** (level - 1)),
                _TRACE_HYDRATION_RETRY_MAX_SECONDS,
            )
            self._trace_hydration_retry[active_key] = (
                level,
                self._trace_hydration_now() + delay,
            )

    def _trace_detail_still_relevant(self, completion: TraceDetailCompletion) -> bool:
        run = self.store.current_run
        if (
            run is None
            or run.operator_instance_id != completion.operator_instance_id
            or run.run_id != completion.run_id
            or run.created_sequence != completion.created_sequence
        ):
            return False
        node = run.nodes.get(completion.node_id)
        descriptor = node.trace if node is not None else None
        return (
            node is not None
            and node.node_id == completion.node_id
            and descriptor is not None
            and descriptor.revision == completion.descriptor_revision
        )

    def _trace_hydration_now(self) -> float:
        return time.monotonic()

    @staticmethod
    def _trace_detail_completion(
        *,
        attempt: int,
        operator_instance_id: str,
        run_id: str,
        created_sequence: int,
        node_id: str,
        descriptor_revision: int,
        hydrated: TraceDetail | None,
        context_generation: int = 0,
    ) -> TraceDetailCompletion:
        trace_body = None
        if hydrated is not None:
            operator_instance_id = hydrated.operator_instance_id
            run_id = hydrated.run_id
            created_sequence = hydrated.created_sequence
            node_id = hydrated.node_id
            descriptor_revision = hydrated.descriptor_revision
            trace_body = hydrated.trace_body
        return TraceDetailCompletion(
            attempt=attempt,
            operator_instance_id=operator_instance_id,
            run_id=run_id,
            created_sequence=created_sequence,
            node_id=node_id,
            descriptor_revision=descriptor_revision,
            trace_body=trace_body,
            context_generation=context_generation,
        )

    def _hydrate_selected_trace(self) -> None:
        """Fetch the selected finalized trace body without blocking the UI thread."""
        key = self._selected_trace_hydration_key()
        self._sync_trace_hydration_context(key)
        if key is None:
            return
        envelope = self.store.selected_agent_trace_envelope
        if envelope is not None and (
            envelope.trace is not None or envelope.evidence is not None
        ):
            self._trace_hydration_retry.pop(key, None)
            return
        if self._trace_hydration_attempts or self._trace_hydration_closed:
            return
        retry = self._trace_hydration_retry.get(key)
        if retry is not None and self._trace_hydration_now() < retry[1]:
            return
        hydrate = self.store.provider.hydrate_trace
        run = self.store.current_run
        if run is None:
            return
        operator_instance_id = run.operator_instance_id
        created_sequence = run.created_sequence
        self._trace_hydration_in_flight.add(key)
        self._trace_hydration_attempt_counter += 1
        attempt = self._trace_hydration_attempt_counter
        self._trace_hydration_attempts[key] = attempt
        run_id, node_id, descriptor_revision = key
        context_generation = self.store._run_interaction_generation

        def _hydrate() -> None:
            hydrated = None
            failure = None
            try:
                hydrated = hydrate(run_id, node_id)
            except Exception as error:
                if not (
                    isinstance(error, OperatorCallError)
                    and error.status
                    in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}
                ):
                    failure = error
            finally:
                completion = self._trace_detail_completion(
                    attempt=attempt,
                    operator_instance_id=operator_instance_id,
                    run_id=run_id,
                    created_sequence=created_sequence,
                    node_id=node_id,
                    descriptor_revision=descriptor_revision,
                    hydrated=hydrated,
                    context_generation=context_generation,
                )
                if failure is None:
                    self.store.enqueue_trace_hydration_completion(completion)
                else:
                    # Fatal details must not enter the transport-retry reducer.
                    self.call_later(self._raise_trace_hydration_error, failure, completion)

        self._trace_hydration_executor.submit(_hydrate)

    def _raise_trace_hydration_error(
        self, error: Exception, completion: TraceDetailCompletion
    ) -> None:
        self._trace_hydration_closed = True
        key = (completion.run_id, completion.node_id, completion.descriptor_revision)
        active_attempt = self._trace_hydration_attempts.get(key)
        if active_attempt is None or active_attempt == completion.attempt:
            self._release_trace_hydration_attempt(completion.attempt)
            self._trace_hydration_retry.pop(key, None)
        raise error

    # ── Connection monitoring ───────────────────────────────────────

    def _check_connection(self) -> None:
        """Reserve the disconnect overlay for an unreachable operator."""
        provider = self.store.provider
        reachable = get_operator_reachability(provider)
        if reachable and self._operator_was_reachable is False:
            self.store._refresh_workflow_catalog()
        self._operator_was_reachable = reachable

        try:
            wrapper = self._screen.query_one("#disconnect-wrapper")
            box = self._screen.query_one("#disconnect-box")
        except Exception:
            return

        if reachable:
            wrapper.remove_class("visible")
            return

        from rich.style import Style
        from rich.text import Text

        dots = "." * ((self.store.frame // 8) % 4)
        msg = Text()
        msg.append("CONNECTION LOST\n\n", Style(color="#f06080", bold=True))
        connection_label = getattr(provider, "connection_label", "Operator")
        last_error = getattr(provider, "last_error", "") or provider.stream_error
        msg.append(f"{connection_label}\n", Style(color="#e0f8ff"))
        msg.append("is not reachable.\n\n", Style(color="#7ab0c8"))
        if last_error:
            msg.append(f"{last_error}\n\n", Style(color="#f0a080"))
        msg.append(f"Reconnecting{dots:<3}", Style(color="#7ab0c8"))
        box.update(msg)
        wrapper.add_class("visible")

    # ── Helpers ─────────────────────────────────────────────────────

    def _scroll_log_to_match(self) -> None:
        """Scroll the log panel so the current search match is visible."""
        try:
            log_w = self._screen.query_one("#log-content", LogWidget)
            log_w.scroll_to_match()
        except Exception:
            pass

    def _scroll_run_history_to_selected(self) -> None:
        """Scroll the run history so the selected run is visible."""
        try:
            rh_w = self._screen.query_one("#run-history-content", RunHistoryWidget)
            rh_w.scroll_to_selected()
        except Exception:
            pass

    def select_node(self, node: DagNode) -> None:
        """Called when a node is clicked in the DAG."""
        self.store.focused_pane = "dag"
        self.store.select_node(node)
        self._sync_sidebar_focus()
        self._refresh_widgets()

    def _visible_panes(self) -> list[str]:
        """Return panes that can receive navigation in the current layout."""
        if self.store.trace_inspector_open:
            return ["trace"]

        panes = ["run-history"]
        if self._dag_visible:
            panes.append("dag")
        if self._logs_visible:
            panes.append("log")
        if self.store.sidebar_visible:
            panes.insert(0, "sidebar")
        return panes

    def _normalize_focused_pane(self) -> None:
        """Move focus out of collapsed dashboard panes."""
        if (
            self._restore_dag_focus
            and not self._dag_visible
            and self.store.focused_pane != "run-history"
        ):
            self._restore_dag_focus = False
        if (
            self._restore_log_focus
            and not self._logs_visible
            and self.store.focused_pane != "run-history"
        ):
            self._restore_log_focus = False
        if self.store.focused_pane in {"sidebar", "trace"}:
            return
        panes = self._visible_panes()
        if self.store.focused_pane not in panes:
            self.store.focused_pane = panes[0]

    def _cycle_visible_panes(self, direction: int) -> None:
        """Cycle focus among panes visible in the current layout."""
        panes = self._visible_panes()
        if self.store.focused_pane not in panes:
            self.store.focused_pane = panes[0]
        current = panes.index(self.store.focused_pane)
        self.store.focused_pane = panes[(current + direction) % len(panes)]

    # ── Pane focus ─────────────────────────────────────────────────

    def action_focus_next_pane(self) -> None:
        self._cycle_visible_panes(1)
        self._sync_sidebar_focus()
        self._refresh_widgets()

    def action_focus_prev_pane(self) -> None:
        self._cycle_visible_panes(-1)
        self._sync_sidebar_focus()
        self._refresh_widgets()

    # ── Navigation (pane-aware) ────────────────────────────────────

    def action_nav_up(self) -> None:
        pane = self.store.focused_pane
        if pane == "sidebar":
            self._screen.query_one("#sidebar", Sidebar).cursor_up()
        elif pane == "dag":
            self.store.move_nav(0, -1)
            self._refresh_widgets()
        elif pane == "trace":
            self.store.move_trace_turn(-1)
            self._refresh_widgets()
            self._scroll_trace_selection_into_view()
        elif pane == "run-history":
            self.store.select_prev_run()
            self._refresh_widgets()
            self._scroll_run_history_to_selected()
        elif pane == "log":
            self._log_autoscroll = False
            try:
                log_view = self._screen.query_one("#log-content", LogWidget)
                log_view.scroll_up(animate=False)
            except Exception:
                pass

    def action_nav_down(self) -> None:
        pane = self.store.focused_pane
        if pane == "sidebar":
            self._screen.query_one("#sidebar", Sidebar).cursor_down()
        elif pane == "dag":
            self.store.move_nav(0, 1)
            self._refresh_widgets()
        elif pane == "trace":
            self.store.move_trace_turn(1)
            self._refresh_widgets()
            self._scroll_trace_selection_into_view()
        elif pane == "run-history":
            self.store.select_next_run()
            self._refresh_widgets()
            self._scroll_run_history_to_selected()
        elif pane == "log":
            try:
                log_view = self._screen.query_one("#log-content", LogWidget)
                log_view.scroll_down(animate=False)
                self._log_autoscroll = log_view.scroll_y >= log_view.max_scroll_y - 1
            except Exception:
                pass

    def action_run_prev(self) -> None:
        """Alt+Up: select previous (newer) run without changing pane focus."""
        self.store.select_prev_run()
        self._refresh_widgets()
        self._scroll_run_history_to_selected()

    def action_run_next(self) -> None:
        """Alt+Down: select next (older) run without changing pane focus."""
        self.store.select_next_run()
        self._refresh_widgets()
        self._scroll_run_history_to_selected()

    def _workflows_in_sidebar_order(self) -> list[WorkflowInfo]:
        if self._screen:
            try:
                sidebar = self._screen.query_one("#sidebar", Sidebar)
                sidebar._rebuild_tree()
                return [
                    item.workflow for item in sidebar._flat_items if item.workflow is not None
                ]
            except Exception:
                pass
        return list(self.store.workflows)

    def action_workflow_prev(self) -> None:
        """Shift+Tab: switch to the previous workflow in explorer order."""
        workflows = self._workflows_in_sidebar_order()
        if not workflows:
            return
        current = self.store.current_workflow
        if current is None:
            self.store.switch_workflow(workflows[0])
        else:
            idx = next(
                (i for i, p in enumerate(workflows) if p.selector == current.selector), 0
            )
            new_idx = (idx - 1) % len(workflows)
            self.store.switch_workflow(workflows[new_idx])
        try:
            self._screen._remount_dag()
        except Exception:
            pass
        self._refresh_widgets()

    def action_workflow_next(self) -> None:
        """Tab: switch to the next workflow in explorer order."""
        workflows = self._workflows_in_sidebar_order()
        if not workflows:
            return
        current = self.store.current_workflow
        if current is None:
            self.store.switch_workflow(workflows[0])
        else:
            idx = next(
                (i for i, p in enumerate(workflows) if p.selector == current.selector), 0
            )
            new_idx = (idx + 1) % len(workflows)
            self.store.switch_workflow(workflows[new_idx])
        try:
            self._screen._remount_dag()
        except Exception:
            pass
        self._refresh_widgets()

    def _scroll_trace_selection_into_view(self) -> None:
        """Keep hierarchy selection visible without forcing a viewport redraw."""
        if not self._screen:
            return
        try:
            if self.store.trace_inspector_tab == "trace":
                self._screen.query_one(
                    "#agent-trace-content", AgentTraceInspector
                ).scroll_selected_into_view()
            elif self.store.trace_inspector_tab == "output":
                self._screen.query_one(
                    "#agent-output-content", AgentOutputInspector
                ).scroll_selected_into_view()
            else:
                self._screen.query_one(
                    "#agent-metadata-content", AgentMetadataInspector
                ).scroll_selected_into_view()
        except Exception:
            pass

    def _move_trace_inspector_tab(self, delta: int) -> None:
        self.store.move_trace_inspector_tab(delta)
        self._refresh_widgets()
        try:
            self._screen.query_one("#agent-trace-inspector").scroll_home(animate=False)
        except Exception:
            pass

    def action_nav_left(self) -> None:
        if self.store.focused_pane == "trace":
            self._move_trace_inspector_tab(-1)
            return
        self.store.move_nav(-1, 0)
        self._refresh_widgets()

    def action_nav_right(self) -> None:
        if self.store.focused_pane == "trace":
            self._move_trace_inspector_tab(1)
            return
        self.store.move_nav(1, 0)
        self._refresh_widgets()

    def action_activate(self) -> None:
        """Enter: activate the focused item or collapse the selected agent turn."""
        if self.store.focused_pane == "sidebar":
            self._screen.query_one("#sidebar", Sidebar).activate_cursor()
        elif self.store.focused_pane == "trace":
            self.store.toggle_trace_turn()
            self._refresh_widgets()
            self._scroll_trace_selection_into_view()
        elif self.store.focused_pane == "dag" and self.store.open_trace_inspector():
            self._hydrate_selected_trace()
            self._refresh_widgets()
            try:
                self._screen.query_one("#agent-trace-inspector").scroll_end(animate=False)
            except Exception:
                pass

    def action_expand_trace_hierarchy(self) -> None:
        if self.store.focused_pane == "dag" and self._screen is not None:
            self._screen.query_one("#dag-panel").expand_all(True)
            return
        if self.store.focused_pane != "trace":
            return
        self.store.expand_trace_hierarchy()
        self._refresh_widgets()
        self._scroll_trace_selection_into_view()

    def action_collapse_trace_hierarchy(self) -> None:
        if self.store.focused_pane == "dag" and self._screen is not None:
            self._screen.query_one("#dag-panel").expand_all(False)
            return
        if self.store.focused_pane != "trace":
            return
        self.store.collapse_trace_hierarchy()
        self._refresh_widgets()
        self._scroll_trace_selection_into_view()

    # ── Other actions ──────────────────────────────────────────────

    def action_toggle_explorer(self) -> None:
        """Space+E: toggle explorer open/closed. Focuses it when opening."""
        self.store.toggle_sidebar()
        if self.store.sidebar_visible:
            self.store.focused_pane = "sidebar"
        self._sync_sidebar_focus()
        self._refresh_widgets()

    def action_escape_key(self) -> None:
        if self.store.trace_inspector_open:
            self.store.close_trace_inspector()
            self._refresh_widgets()
            return
        if self.store.searching:
            self.store.cancel_search()
        elif self.store.search_query:
            self.store.clear_search()
        elif self.store.focused_pane == "sidebar":
            self.store.focused_pane = "dag"
            self._sync_sidebar_focus()
        elif self.store.selected_node:
            self.store.deselect_node()
        elif self.store.run_pinned:
            self.store.deselect_run()
        self._refresh_widgets()

    def action_start_run(self) -> None:
        session = self.extension_session
        if session is not None:
            description = session.description
            action = (
                next((a for a in description.actions if a.id == "run.start"), None)
                if description
                else None
            )
            if action is None:
                self.notify("Start is not exposed by this provider", severity="warning")
                return
            self.invoke_extension_action(action)
            return
        self._run_actions_menu_open = False
        if not self.can_start_run():
            return
        if self.store.start_run_async():
            self._log_autoscroll = True
            self._refresh_widgets()

    def can_start_run(self) -> bool:
        """Return whether the current workflow can start a run."""
        if self.extension_session is not None:
            description = self.extension_session.description
            return (
                not self.read_only
                and description is not None
                and any(a.id == "run.start" and a.available for a in description.actions)
            )
        return (
            not self.read_only
            and self.store.current_workflow is not None
            and not self.store._start_run_in_flight
        )

    def can_cancel_selected_run(self) -> bool:
        if self.extension_session is not None:
            description = self.extension_session.description
            return (
                not self.read_only
                and description is not None
                and any(a.id == "run.cancel" and a.available for a in description.actions)
            )
        run = self.store.current_run
        return (
            not self.read_only
            and run is not None
            and run.status in {RunStatus.REQUESTING, RunStatus.PENDING, RunStatus.RUNNING}
            and not self._cancel_run_requests
        )

    def action_cancel_run(self) -> None:
        """Request cancellation without blocking the Textual event loop."""
        if self.extension_session is not None:
            description = self.extension_session.description
            action = (
                next((a for a in description.actions if a.id == "run.cancel"), None)
                if description
                else None
            )
            if action is not None:
                self.invoke_extension_action(action)
            return
        run = self.store.current_run
        if run is None or not self.can_cancel_selected_run():
            return

        run_id = run.run_id
        self._cancel_run_requests.add(run_id)
        self._run_actions_menu_open = False
        self._refresh_widgets()

        def request_cancel() -> None:
            error = ""
            try:
                self.store.provider.cancel_run(run_id)
            except Exception as exc:
                error = str(exc) or "Run failed to stop"
            if not self._run_controls_closed:
                self.call_from_thread(self._complete_cancel_run, run_id, error)

        self._run_control_executor.submit(request_cancel)

    def _complete_cancel_run(self, run_id: str, error: str) -> None:
        """Apply the completed cancellation request on the UI thread."""
        self._cancel_run_requests.discard(run_id)
        if error:
            self.store.run_error = error
        self._refresh_widgets()

    def action_toggle_run_actions_menu(self) -> None:
        if (self._screen is not None and self._screen.size.height <= 15) or (
            not self.can_start_run() and not self.can_cancel_selected_run()
        ):
            return
        self._run_actions_menu_open = not self._run_actions_menu_open
        self._refresh_widgets()

    def action_toggle_dag(self) -> None:
        """d: hide or show the DAG without remounting it."""
        if self._dag_visible:
            self._dag_visible = False
            self._restore_dag_focus = self.store.focused_pane == "dag"
            if self._restore_dag_focus:
                self.store.focused_pane = "run-history"
        else:
            self._dag_visible = True
            if self._restore_dag_focus and self.store.focused_pane == "run-history":
                self.store.focused_pane = "dag"
            self._restore_dag_focus = False
        self._refresh_widgets()

    def action_toggle_logs(self) -> None:
        """l: hide or show logs without remounting their content."""
        if self._logs_visible:
            self._logs_visible = False
            self._restore_log_focus = self.store.focused_pane == "log"
            if self._restore_log_focus:
                self.store.focused_pane = "run-history"
        else:
            self._logs_visible = True
            if self._restore_log_focus and self.store.focused_pane == "run-history":
                self.store.focused_pane = "log"
            self._restore_log_focus = False
        self._refresh_widgets()

    def action_toggle_autoscroll(self) -> None:
        """s: toggle log autoscroll."""
        self._log_autoscroll = not self._log_autoscroll
        self._refresh_widgets()

    def action_toggle_wrap(self) -> None:
        """w: toggle log word-wrap."""
        self._log_wrap = not self._log_wrap
        self._refresh_widgets()

    def action_log_page_up(self) -> None:
        """Page Up: scroll log panel up, disable autoscroll."""
        if self.store.trace_inspector_open:
            try:
                self._screen.query_one("#agent-trace-inspector").scroll_page_up(animate=False)
            except Exception:
                pass
            return
        self._log_autoscroll = False
        try:
            log_view = self._screen.query_one("#log-content", LogWidget)
            log_view.scroll_page_up(animate=False)
        except Exception:
            pass

    def action_log_page_down(self) -> None:
        """Page Down: scroll log panel down; re-enable autoscroll at bottom."""
        if self.store.trace_inspector_open:
            try:
                self._screen.query_one("#agent-trace-inspector").scroll_page_down(animate=False)
            except Exception:
                pass
            return
        try:
            log_view = self._screen.query_one("#log-content", LogWidget)
            log_view.scroll_page_down(animate=False)
            self._log_autoscroll = log_view.scroll_y >= log_view.max_scroll_y - 1
        except Exception:
            pass

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self.store.searching and action not in {"escape_key", "quit"}:
            return False
        if self._screen is not None and self.screen is not self._screen and action != "quit":
            return False
        return True

    def on_key(self, event) -> None:
        """Handle search input, leader key (space), and / n N keys."""
        if self._screen is not None and self.screen is not self._screen:
            return
        if self.store.searching:
            if event.key == "escape":
                return
            if event.key == "enter":
                self.store.end_search()
                self._refresh_widgets()
                self._scroll_log_to_match()
            elif event.key == "backspace":
                self.store.search_backspace()
            elif event.is_printable and event.character:
                self.store.search_append(event.character)
            event.prevent_default()
            event.stop()
            self._refresh_widgets()
            return

        if (
            event.character == "o"
            and self.store.trace_inspector_open
            and self.store.trace_inspector_tab == "trace"
        ):
            self.store.toggle_trace_full_output()
            event.prevent_default()
            event.stop()
            self._refresh_widgets()
            self._scroll_trace_selection_into_view()
            return

        # Leader key: space → {e}
        if self._leader_pending:
            self._leader_pending = False
            if event.character == "e":
                self.action_toggle_explorer()
                event.prevent_default()
                event.stop()
                return
            # Not a known leader sequence — ignore both keys

        if event.key == "space":
            self._leader_pending = True
            event.prevent_default()
            event.stop()
            return

        if event.character == "/":
            self.store.begin_search()
            event.prevent_default()
            event.stop()
            self._refresh_widgets()
        elif event.character == "n" and self.store.search_query:
            self.store.search_next()
            event.prevent_default()
            event.stop()
            self._refresh_widgets()
            self._scroll_log_to_match()
        elif event.character == "N" and self.store.search_query:
            self.store.search_prev()
            event.prevent_default()
            event.stop()
            self._refresh_widgets()
            self._scroll_log_to_match()
