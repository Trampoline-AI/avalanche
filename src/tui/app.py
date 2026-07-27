"""AvalancheApp — main TUI application backed by StateProvider."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Header

from .dag_layout import DagNode
from .mock import MockStateProvider
from .models import RunState, RunStatus, TraceDetail, WorkflowInfo
from .screens.workflow_detail import WorkflowDetailScreen
from .state import StateProvider, get_operator_reachability
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
        Binding("d", "toggle_dag", "DAG", priority=True),
        Binding("l", "toggle_logs", "Logs", priority=True),
        ("enter", "activate", "Enter"),
    ]

    def __init__(
        self,
        provider: StateProvider | None = None,
        workflow: str | None = None,
        node: str | None = None,
        *,
        close_provider_on_unmount: bool = True,
    ):
        super().__init__()
        self.register_theme(AVALANCHE_THEME)
        self.theme = "avalanche"
        defer_initial_catalog = provider is not None
        if provider is None:
            provider = MockStateProvider(include_agent_trace=workflow == "agent_trace")
        self.store = UIStore(provider, defer_initial_catalog=defer_initial_catalog)
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
        self._logs_visible = True
        self._run_actions_menu_open = False
        self._trace_hydration_retry: dict[TraceHydrationKey, tuple[int, float]] = {}
        self._trace_hydration_context: TraceHydrationKey | None = None
        self._deep_link_workflow = workflow
        self._deep_link_node = node
        self._operator_was_reachable: bool | None = None
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
        self.store.provider.on_detail_update(self.store.enqueue_detail_update)
        self.store.provider.on_log(lambda _: None)
        self.store.provider.start_stream()

        self._timer = self.set_interval(1 / 30, self._tick)

    def on_unmount(self) -> None:
        """Cancel provider work and join both hydration workers."""
        if self._trace_hydration_closed:
            return
        self._trace_hydration_closed = True
        self._run_controls_closed = True
        self.store.request_shutdown()
        close = getattr(self.store.provider, "close", None)
        try:
            if self._close_provider_on_unmount and callable(close):
                close()
        finally:
            self.store.shutdown()
            self._trace_hydration_executor.shutdown(wait=True, cancel_futures=True)
            self._run_control_executor.shutdown(wait=True, cancel_futures=True)

    def _on_run_update_bg(self, run: RunState) -> None:
        """Called from background thread when a run state changes."""
        self.store.enqueue_run_update(run)

    # ── Tick ───────────────────────────────────────────────────────

    _poll_counter: int = 0

    def _tick(self) -> None:
        catalog_revision = self.store.catalog_revision
        self.store.tick()
        self._apply_trace_hydration_completions()
        self._hydrate_selected_trace()
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
        except Exception:
            pass
        try:
            self._screen.sync_controls()
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
        workflow = self.store.current_workflow
        if run is None or workflow is None or self._poll_in_flight:
            return
        # Only poll for active/recent runs
        provider = self.store.provider
        run_id = run.run_id
        selector = workflow.selector
        data_revision = self.store._run_data_revision(selector)
        context_epoch = self.store._workflow_context_epoch
        self._poll_in_flight = True

        def _do_poll():
            try:
                fresh = provider.get_run(run_id)
                if fresh is not None:
                    self.store.enqueue_polled_run_update(
                        selector,
                        data_revision,
                        context_epoch,
                        fresh,
                    )
            except Exception:
                pass
            finally:
                self._poll_in_flight = False

        threading.Thread(target=_do_poll, daemon=True).start()

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

    def _apply_trace_hydration_completions(self) -> None:
        for completion, applied in self.store.take_trace_hydration_completions():
            active_key = next(
                (
                    key
                    for key, attempt in self._trace_hydration_attempts.items()
                    if attempt == completion.attempt
                ),
                None,
            )
            if active_key is None:
                continue
            self._trace_hydration_attempts.pop(active_key, None)
            self._trace_hydration_in_flight.discard(active_key)
            self._trace_hydration_superseded.discard(completion.attempt)
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
        )

    def _hydrate_selected_trace(self) -> None:
        """Fetch the selected finalized trace body without blocking the UI thread."""
        key = self._selected_trace_hydration_key()
        self._sync_trace_hydration_context(key)
        if key is None:
            return
        envelope = self.store.selected_agent_trace_envelope
        if envelope is not None and isinstance(envelope.get("trace"), dict):
            self._trace_hydration_retry.pop(key, None)
            return
        if self._trace_hydration_attempts or self._trace_hydration_closed:
            return
        retry = self._trace_hydration_retry.get(key)
        if retry is not None and self._trace_hydration_now() < retry[1]:
            return
        hydrate = getattr(self.store.provider, "hydrate_trace", None)
        if not callable(hydrate):
            return
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

        def _hydrate() -> None:
            hydrated = None
            try:
                hydrated = hydrate(run_id, node_id)
            except Exception:
                pass
            finally:
                completion = self._trace_detail_completion(
                    attempt=attempt,
                    operator_instance_id=operator_instance_id,
                    run_id=run_id,
                    created_sequence=created_sequence,
                    node_id=node_id,
                    descriptor_revision=descriptor_revision,
                    hydrated=hydrated,
                )
                self.store.enqueue_trace_hydration_completion(completion)

        self._trace_hydration_executor.submit(_hydrate)

    # ── Connection monitoring ───────────────────────────────────────

    _ping_counter: int = 0
    _ping_in_flight: bool = False

    def _check_connection(self) -> None:
        """Reserve the disconnect overlay for an unreachable operator."""
        provider = self.store.provider
        ping = getattr(provider, "ping", None)
        if not callable(ping):
            return  # Local providers do not expose connection tracking.

        # Ping every ~2s (30 ticks at 15fps), non-blocking.
        self._ping_counter += 1
        if self._ping_counter % 60 == 0 and not self._ping_in_flight:
            self._ping_in_flight = True

            def _do_ping():
                try:
                    ping()
                finally:
                    self._ping_in_flight = False

            threading.Thread(target=_do_ping, daemon=True).start()

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
        self._run_actions_menu_open = False
        if self.store.start_run_async():
            self._log_autoscroll = True
            self._refresh_widgets()

    def can_cancel_selected_run(self) -> bool:
        run = self.store.current_run
        return (
            run is not None
            and run.status in {RunStatus.PENDING, RunStatus.RUNNING}
            and run.run_id not in self._cancel_run_requests
        )

    def action_cancel_run(self) -> None:
        """Request cancellation without blocking the Textual event loop."""
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
        if self._screen is not None and self._screen.size.height <= 15:
            return
        self._run_actions_menu_open = not self._run_actions_menu_open
        self._refresh_widgets()

    def action_toggle_dag(self) -> None:
        """d: hide or show the DAG without remounting it."""
        self._dag_visible = not self._dag_visible
        if not self._dag_visible and self.store.focused_pane == "dag":
            self.store.focused_pane = "run-history"
        self._refresh_widgets()

    def action_toggle_logs(self) -> None:
        """l: hide or show logs without remounting their content."""
        self._logs_visible = not self._logs_visible
        if not self._logs_visible and self.store.focused_pane == "log":
            self.store.focused_pane = "run-history"
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
