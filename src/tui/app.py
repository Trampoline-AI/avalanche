"""AvalancheApp — main TUI application backed by StateProvider."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Header

from .dag_layout import DagNode
from .mock import MockStateProvider
from .models import RunState, WorkflowInfo
from .screens.workflow_detail import WorkflowDetailScreen
from .state import ConnectionAwareStateProvider, StateProvider
from .theme import AVALANCHE_THEME
from .ui_store import UIStore
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
        ("escape", "escape_key", "Esc"),
        Binding("tab", "workflow_next", "Tab", priority=True),
        Binding("shift+tab", "workflow_prev", "Shift+Tab", priority=True),
        ("r", "start_run", "Run"),
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
        ("enter", "activate", "Enter"),
    ]

    def __init__(
        self,
        provider: StateProvider | None = None,
        workflow: str | None = None,
        node: str | None = None,
    ):
        super().__init__()
        self.register_theme(AVALANCHE_THEME)
        self.theme = "avalanche"
        defer_initial_catalog = provider is not None
        if provider is None:
            provider = MockStateProvider(include_agent_trace=workflow == "agent_trace")
        self.store = UIStore(provider, defer_initial_catalog=defer_initial_catalog)
        self._timer: Timer | None = None
        self._screen: WorkflowDetailScreen | None = None
        self._leader_pending: bool = False
        self._log_autoscroll: bool = True
        self._log_wrap: bool = False
        self._deep_link_workflow = workflow
        self._deep_link_node = node
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
        if node:
            match = next((n for n in self.store.all_nodes if n.name == node), None)
            if match is None:
                match = next((n for n in self.store.all_nodes if n.display_name == node), None)
            if match:
                self.store.select_node(match)
                self._deep_link_node = None

    def compose(self) -> ComposeResult:
        yield Header()

    def on_mount(self) -> None:
        self._screen = WorkflowDetailScreen()
        self.push_screen(self._screen)

        self.store.provider.on_run_update(self._on_run_update_bg)
        self.store.provider.on_log(lambda _: None)

        self._timer = self.set_interval(1 / 30, self._tick)

    def _on_run_update_bg(self, run: RunState) -> None:
        """Called from background thread when a run state changes."""
        self.store.enqueue_run_update(run)

    # ── Tick ───────────────────────────────────────────────────────

    _poll_counter: int = 0

    def _tick(self) -> None:
        catalog_revision = self.store.catalog_revision
        self.store.tick()
        if self.store.catalog_revision != catalog_revision:
            self._apply_deep_link()
            if self._screen:
                self._screen._remount_dag()
        # Poll current run every ~1s as fallback if stream missed updates
        self._poll_counter += 1
        if self._poll_counter % 30 == 0:
            self._poll_current_run()
        self.store.auto_follow_latest_run()
        self._refresh_widgets()
        self._autoscroll_logs()
        self._check_connection()

    def _refresh_widgets(self) -> None:
        """Refresh all visible widgets to reflect current store state."""
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
        except Exception:
            pass
        # Show/hide sidebar + grip, sync width
        visible = self.store.sidebar_visible
        try:
            sidebar = self._screen.query_one("#sidebar", Sidebar)
            sidebar.display = visible
            if visible:
                sidebar.styles.width = self.store.sidebar_width
        except Exception:
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
            except Exception:
                pass
        # Refresh non-pane widgets
        for widget_id in ("status-bar", "dag-panel"):
            try:
                self._screen.query_one(f"#{widget_id}").refresh()
            except Exception:
                pass
        # Run history still sizes itself to its complete table.
        try:
            self._screen.query_one("#run-history-content").refresh(layout=True)
        except Exception:
            pass
        if self.store.trace_inspector_open:
            for content_id in (
                "agent-trace-content",
                "agent-output-content",
                "agent-metadata-content",
            ):
                try:
                    self._screen.query_one(f"#{content_id}").refresh(layout=True)
                except Exception:
                    pass
        # Update sticky headers
        try:
            from .widgets.run_history import RunHistoryWidget

            self._screen.query_one("#run-history-header").update(
                RunHistoryWidget.render_header()
            )
        except Exception:
            pass
        try:
            self._screen.query_one("#log-header").update(LogWidget.render_header())
        except Exception:
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
        except Exception:
            pass
        try:
            from .widgets.run_history import _cron_description, _next_run_label

            rh = self._screen.query_one("#run-history")
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
        except Exception:
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
        except Exception:
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

    # ── Run polling (fallback for missed stream events) ─────────────

    _poll_in_flight: bool = False

    def _poll_current_run(self) -> None:
        """Fetch fresh state for the current run via get_run in a background thread.

        Covers cases where the gRPC stream hasn't connected yet or
        missed updates (e.g. first run while Ray is starting).
        """
        run = self.store.current_run
        if run is None or self._poll_in_flight:
            return
        # Only poll for active/recent runs
        provider = self.store.provider
        run_id = run.run_id

        import threading

        self._poll_in_flight = True

        def _do_poll():
            try:
                fresh = provider.get_run(run_id)
                if fresh is not None:
                    self.store.enqueue_run_update(fresh)
            except Exception:
                pass
            finally:
                self._poll_in_flight = False

        threading.Thread(target=_do_poll, daemon=True).start()

    # ── Connection monitoring ───────────────────────────────────────

    _ping_counter: int = 0
    _ping_in_flight: bool = False

    def _check_connection(self) -> None:
        """Show/hide disconnect overlay based on provider connection state."""
        provider = self.store.provider
        if not isinstance(provider, ConnectionAwareStateProvider):
            return  # MockStateProvider — no connection tracking

        # Ping every ~2s (30 ticks at 15fps), non-blocking
        self._ping_counter += 1
        if self._ping_counter % 60 == 0 and not self._ping_in_flight:
            import threading

            self._ping_in_flight = True

            def _do_ping():
                try:
                    provider.ping()
                finally:
                    self._ping_in_flight = False

            threading.Thread(target=_do_ping, daemon=True).start()

        try:
            wrapper = self._screen.query_one("#disconnect-wrapper")
            box = self._screen.query_one("#disconnect-box")
        except Exception:
            return

        if provider.connected:
            if wrapper.has_class("visible"):
                # Just reconnected — refresh workflow list
                wrapper.remove_class("visible")
                self.store._refresh_workflow_catalog()
        else:
            from rich.style import Style
            from rich.text import Text

            dots = "." * ((self.store.frame // 8) % 4)

            msg = Text()
            msg.append("CONNECTION LOST\n\n", Style(color="#f06080", bold=True))
            msg.append(f"{provider.connection_label}\n", Style(color="#e0f8ff"))
            msg.append("is not reachable.\n\n", Style(color="#7ab0c8"))
            if provider.last_error:
                msg.append(f"{provider.last_error}\n\n", Style(color="#f0a080"))
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

    # ── Pane focus ─────────────────────────────────────────────────

    def action_focus_next_pane(self) -> None:
        self.store.cycle_pane(1)
        self._sync_sidebar_focus()
        self._refresh_widgets()

    def action_focus_prev_pane(self) -> None:
        self.store.cycle_pane(-1)
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
        elif pane == "trace" and self.store.trace_inspector_tab == "trace":
            self.store.move_trace_turn(-1)
            self._refresh_widgets()
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
        elif pane == "trace" and self.store.trace_inspector_tab == "trace":
            self.store.move_trace_turn(1)
            self._refresh_widgets()
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
        elif self.store.focused_pane == "trace" and self.store.trace_inspector_tab == "trace":
            self.store.toggle_trace_turn()
            self._refresh_widgets()
        elif self.store.focused_pane == "dag" and self.store.open_trace_inspector():
            self._refresh_widgets()
            try:
                self._screen.query_one("#agent-trace-inspector").scroll_end(animate=False)
            except Exception:
                pass

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
        if self.store.start_run_async():
            self._log_autoscroll = True
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

    def on_key(self, event) -> None:
        """Handle search input, leader key (space), and / n N keys."""
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
