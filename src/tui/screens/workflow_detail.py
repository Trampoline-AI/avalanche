"""WorkflowDetail screen — sidebar + two-pane layout with DAG and run history."""

from __future__ import annotations

from rich.color import Color
from rich.segment import Segments
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.scrollbar import ScrollBar, ScrollBarRender
from textual.widgets import Button, Header, Static

from ..widgets.agent_trace import (
    AgentMetadataInspector,
    AgentOutputInspector,
    AgentTraceInspector,
)
from ..widgets.dag import DagWidget
from ..widgets.log_panel import LogWidget
from ..widgets.run_history import RunHistoryWidget
from ..widgets.sidebar import Sidebar
from ..widgets.status_bar import StatusBar


class _HalfHeightScrollBarRender(ScrollBarRender):
    """Renders horizontal scrollbar using lower-half blocks for a thinner look.

    Overrides render_bar to use ▄ for both track and thumb, with color
    tricks so the entire scrollbar occupies only the bottom half of the cell.
    """

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color | None = None,
        bar_color: Color | None = None,
    ) -> Segments:
        from rich.segment import Segment
        from rich.style import Style

        if back_color is None:
            back_color = Color.parse("#555555")
        if bar_color is None:
            bar_color = Color.parse("bright_magenta")

        if vertical:
            return super().render_bar(
                size=size,
                virtual_size=virtual_size,
                window_size=window_size,
                position=position,
                thickness=thickness,
                vertical=vertical,
                back_color=back_color,
                bar_color=bar_color,
            )

        upper = {"@mouse.down": "scroll_up"}
        lower = {"@mouse.down": "scroll_down"}
        foreground_meta = {"@mouse.down": "grab"}

        track_style_upper = Style(color=back_color, meta=upper)
        track_style_lower = Style(color=back_color, meta=lower)
        thumb_style = Style(color=bar_color, meta=foreground_meta)

        segments = [Segment("▃", track_style_upper)] * int(size)

        if window_size and size and virtual_size and size != virtual_size:
            from math import ceil

            bar_ratio = virtual_size / size
            thumb_size = max(1, window_size / bar_ratio)
            position_ratio = position / (virtual_size - window_size)
            thumb_pos = (size - thumb_size) * position_ratio
            start = int(thumb_pos)
            end = min(int(ceil(thumb_pos + thumb_size)), size)

            segments[end:] = [Segment("▃", track_style_lower)] * (size - end)
            segments[start:end] = [Segment("▃", thumb_style)] * (end - start)

        return Segments((segments + [Segment.line()]) * thickness, new_lines=False)


class _ThinScrollContainer(ScrollableContainer):
    """ScrollableContainer with 1-cell, half-height scrollbars.

    Arrow key bindings are removed so the app's pane-aware navigation
    handles all arrow key behavior.
    """

    DEFAULT_CSS = """
    _ThinScrollContainer {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    """

    @property
    def scrollbar_size_vertical(self) -> int:
        return 1 if self.show_vertical_scrollbar else 0

    @property
    def scrollbar_size_horizontal(self) -> int:
        return 1 if self.show_horizontal_scrollbar else 0

    @property
    def horizontal_scrollbar(self) -> ScrollBar:
        sb = super().horizontal_scrollbar
        sb.renderer = _HalfHeightScrollBarRender
        return sb


class _DagScrollContainer(_ThinScrollContainer):
    """DAG viewport with cumulative, animated pointer scrolling."""

    HORIZONTAL_SCROLL_STEP = 8
    VERTICAL_SCROLL_STEP = 3
    SCROLL_DURATION = 0.08

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        self._scroll_horizontal(event, -1)

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        self._scroll_horizontal(event, 1)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if event.ctrl or event.shift:
            self._scroll_horizontal(event, -1)
        else:
            self._scroll_vertical(event, -1)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.ctrl or event.shift:
            self._scroll_horizontal(event, 1)
        else:
            self._scroll_vertical(event, 1)

    def _scroll_horizontal(
        self,
        event: events.MouseEvent,
        direction: int,
    ) -> None:
        if not self.allow_horizontal_scroll:
            return
        changed = self._scroll_to(
            x=self.scroll_target_x + direction * self.HORIZONTAL_SCROLL_STEP,
            animate=True,
            duration=self.SCROLL_DURATION,
            easing="out_cubic",
        )
        if changed:
            event.stop()

    def _scroll_vertical(
        self,
        event: events.MouseEvent,
        direction: int,
    ) -> None:
        if not self.allow_vertical_scroll:
            return
        changed = self._scroll_to(
            y=self.scroll_target_y + direction * self.VERTICAL_SCROLL_STEP,
            animate=True,
            duration=self.SCROLL_DURATION,
            easing="out_cubic",
        )
        if changed:
            event.stop()


class _TableScrollContainer(_ThinScrollContainer):
    """Scroll container for table panes with sticky header sync."""

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        try:
            self.screen._sync_header_for(self)
        except Exception:
            pass


class _DagCenterBtn(Static):
    """Clickable button that re-centers the DAG in its scroll container."""

    DEFAULT_CSS = """
    _DagCenterBtn {
        width: auto;
        height: 1;
        background: transparent;
    }
    """

    def on_click(self, event) -> None:
        event.stop()
        try:
            container = self.screen.query_one("#dag-container")
            container.scroll_to(x=0, y=0, animate=False)
        except Exception:
            pass


class WorkflowDetailScreen(Screen):
    """Two-pane layout: sidebar on left, DAG + run history + logs on right."""

    AUTO_FOCUS = ""  # Disable auto-focus so DAG navigation works immediately
    _control_state: tuple[bool, bool, bool, bool, bool, bool, int] | None = None

    CSS = """
    WorkflowDetailScreen {
        layout: vertical;
        layers: default overlay;
    }
    Header {
        dock: top;
        height: 1;
    }
    #main-layout {
        height: 1fr;
    }
    #right-pane {
        width: 1fr;
        height: 100%;
    }

    /* ── Shared border title styling ── */
    #sidebar, #dag-container, #run-history, #log-panel, #agent-trace-inspector {
        border: solid #5a4f80;
        border-title-color: $accent;
        border-title-style: bold;
        border-title-align: left;
    }
    #sidebar.-pane-active,
    #dag-container.-pane-active,
    #run-history.-pane-active,
    #log-panel.-pane-active,
    #agent-trace-inspector.-pane-active {
        border: solid $accent;
    }

    #dashboard-pane, #agent-trace-inspector {
        width: 100%;
        height: 100%;
    }
    #dag-section, #log-section {
        height: 2fr;
    }
    #dag-section.-collapsed, #log-section.-collapsed {
        height: 1;
    }
    .pane-controls {
        height: 1;
        width: 100%;
        background: $panel;
    }
    .pane-controls Button {
        height: 1;
        min-height: 1;
        min-width: 0;
        padding: 0 1;
    }
    .control-hint {
        width: 1fr;
        content-align: right middle;
        color: $text-muted;
    }
    #run-toolbar {
        dock: top;
        height: 4;
    }
    #run-toolbar.-menu-open {
        height: 8;
    }
    #run-controls {
        height: 2;
    }
    #run-action-menu {
        display: none;
        height: 4;
        background: $panel;
    }
    #run-action-menu.-open {
        display: block;
    }
    #run-action-menu Button {
        height: 1;
        min-height: 1;
        width: 100%;
        min-width: 0;
        padding: 0 1;
    }

    #agent-trace-inspector {
        display: none;
    }
    #agent-output-content, #agent-metadata-content {
        display: none;
    }

    /* ── Sidebar ── */
    #sidebar {
        height: 100%;
        padding: 0;
        overflow-x: hidden;
        overflow-y: auto;
    }

    /* ── DAG container ── */
    #dag-container {
        height: 2fr;
        align: center middle;
    }
    #dag-center-btn {
        dock: bottom;
        height: 1;
        width: 100%;
        content-align: right middle;
    }

    /* ── DAG panel ── */
    #dag-panel {
        width: auto;
        height: auto;
        min-height: 3;
        margin-top: 1;
    }

    /* ── Runs ── */
    #run-history {
        height: 1fr;
        min-height: 8;
    }
    #run-history.-actions-open {
        height: 12;
        min-height: 12;
    }
    #run-history.-compact {
        min-height: 6;
    }
    #run-history.-compact #run-history-content {
        display: none;
    }
    .pane-header {
        dock: top;
        height: 2;
        width: 100%;
        background: $background;
    }
    #run-history-header {
        dock: none;
    }
    #log-section.-compact {
        height: 3;
        min-height: 3;
    }
    /* ── Log panel ── */
    #log-panel {
        height: 2fr;
    }

    /* ── Scrollable content ── */
    #run-history-content {
        height: auto;
        width: auto;
    }
    #log-content {
        height: 1fr;
        width: 1fr;
    }

    /* ── Status bar ── */
    #status-bar {
        height: 1;
        dock: bottom;
        background: $primary-background;
    }

    /* ── Disconnect modal ── */
    #disconnect-wrapper {
        display: none;
        layer: overlay;
        width: 100%;
        height: 100%;
        align: center middle;
    }
    #disconnect-wrapper.visible {
        display: block;
    }
    #disconnect-box {
        width: 48;
        height: auto;
        max-height: 12;
        border: solid #f06080;
        background: #1a1230;
        padding: 1 2;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            yield Sidebar(id="sidebar")
            with Vertical(id="right-pane"):
                with Vertical(id="dashboard-pane"):
                    with _TableScrollContainer(id="run-history") as rh:
                        rh.border_title = "Runs"
                        with Vertical(id="run-toolbar"):
                            with Horizontal(id="run-controls", classes="pane-controls"):
                                yield Button("Start run (r)", id="run-start-button")
                                yield Button("Stop selected", id="run-stop-button")
                                yield Button("Actions ▾", id="run-actions-button")
                                yield Static("↑↓ select", classes="control-hint")
                            with Vertical(id="run-action-menu"):
                                yield Button("Start run (r)", id="run-menu-start-button")
                                yield Button("Stop selected run", id="run-menu-stop-button")
                            yield Static(id="run-history-header", classes="pane-header")
                        yield RunHistoryWidget(id="run-history-content")
                    with Vertical(id="dag-section"):
                        with Horizontal(id="dag-controls", classes="pane-controls"):
                            yield Button("Hide DAG (d)", id="dag-toggle-button")
                            yield Static("collapse / restore", classes="control-hint")
                        with _DagScrollContainer(id="dag-container") as dag_container:
                            dag_container.border_title = "DAG"
                            dag_container.styles.height = "1fr"
                            yield DagWidget(id="dag-panel")
                            yield _DagCenterBtn(" ⊡ center ", id="dag-center-btn")
                    with Vertical(id="log-section"):
                        with Horizontal(id="log-controls", classes="pane-controls"):
                            yield Button("Hide Logs (l)", id="log-toggle-button")
                            yield Static("collapse / restore", classes="control-hint")
                        with Vertical(id="log-panel") as lp:
                            lp.border_title = "Logs"
                            lp.styles.height = "1fr"
                            yield Static(id="log-header", classes="pane-header")
                            yield LogWidget(id="log-content")
                with _ThinScrollContainer(id="agent-trace-inspector") as inspector:
                    inspector.border_title = "Agent"
                    yield AgentTraceInspector(id="agent-trace-content")
                    yield AgentOutputInspector(id="agent-output-content")
                    yield AgentMetadataInspector(id="agent-metadata-content")
        from textual.containers import Container

        with Container(id="disconnect-wrapper"):
            yield Static(id="disconnect-box")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Sidebar)
            sidebar.styles.width = self.app.store.sidebar_width
        except Exception:
            pass

    def sync_controls(self) -> None:
        """Reflect app-owned control state without remounting pane content."""
        app = self.app
        dag_visible = app._dag_visible
        logs_visible = app._logs_visible
        can_start = (
            app.store.current_workflow is not None and not app.store._start_run_in_flight
        )
        can_stop = app.can_cancel_selected_run()
        compact_layout = self.size.height <= 15
        menu_was_open = app._run_actions_menu_open
        if compact_layout:
            app._run_actions_menu_open = False
        menu_open = app._run_actions_menu_open
        control_state = (
            dag_visible,
            logs_visible,
            can_start,
            can_stop,
            menu_open,
            compact_layout,
            self.size.height,
        )
        if control_state == self._control_state and not (compact_layout and menu_was_open):
            return
        self._control_state = control_state

        self.query_one("#dag-section").set_class(not dag_visible, "-collapsed")
        self.query_one("#dag-container").display = dag_visible
        self.query_one("#dag-toggle-button", Button).label = (
            "Hide DAG (d)" if dag_visible else "Show DAG (d)"
        )
        self.query_one("#log-section").set_class(not logs_visible, "-collapsed")
        self.query_one("#log-panel").display = logs_visible
        self.query_one("#log-toggle-button", Button).label = (
            "Hide Logs (l)" if logs_visible else "Show Logs (l)"
        )
        self.query_one("#run-start-button", Button).disabled = not can_start
        self.query_one("#run-stop-button", Button).disabled = not can_stop
        menu_button = self.query_one("#run-actions-button", Button)
        menu_button.disabled = compact_layout or (not can_start and not can_stop)
        self.query_one("#log-section").set_class(compact_layout, "-compact")
        self.query_one("#run-history").set_class(compact_layout, "-compact")
        self.query_one("#run-toolbar").set_class(menu_open, "-menu-open")
        self.query_one("#run-history").set_class(menu_open, "-actions-open")
        menu_button.label = "Actions ▴" if menu_open else "Actions ▾"
        self.query_one("#run-action-menu").set_class(menu_open, "-open")
        self.query_one("#run-menu-start-button", Button).disabled = not can_start
        self.query_one("#run-menu-stop-button", Button).disabled = not can_stop

    def _remount_dag(self) -> None:
        """Replace the DAG widget when workflow changes."""
        try:
            old_dag = self.query_one("#dag-panel", DagWidget)
            new_dag = DagWidget(id="dag-panel")
            old_dag.replace(new_dag)
        except Exception:
            pass

    _HEADER_MAP: dict[str, str] = {
        "run-history": "#run-history-header",
        "log-panel": "#log-header",
    }

    def _sync_header_for(self, container) -> None:
        """Sync a single header for the container that scrolled."""
        hdr_id = self._HEADER_MAP.get(container.id)
        if hdr_id:
            try:
                self.query_one(hdr_id).styles.offset = (-container.scroll_x, 0)
            except Exception:
                pass

    def _sync_header_scroll(self) -> None:
        """Sync all sticky headers with their container scroll positions."""
        for container_id, hdr_id in self._HEADER_MAP.items():
            try:
                hdr = self.query_one(hdr_id)
                container = self.query_one(f"#{container_id}")
                hdr.styles.offset = (-container.scroll_x, 0)
            except Exception:
                pass

    # ── Message handlers ───────────────────────────────────────────

    def on_click(self, event) -> None:
        """Focus the pane that was clicked anywhere in its area."""
        pane_ids = {
            "sidebar": "sidebar",
            "dag-container": "dag",
            "dag-panel": "dag",
            "dag-center-btn": "dag",
            "run-history": "run-history",
            "run-history-header": "run-history",
            "run-history-content": "run-history",
            "log-panel": "log",
            "log-header": "log",
            "log-content": "log",
        }
        widget = event.widget
        # Walk up to find a known pane
        while widget is not None:
            wid = getattr(widget, "id", None)
            if wid in pane_ids:
                self.app.store.focused_pane = pane_ids[wid]
                self.app._sync_sidebar_focus()
                self.app._refresh_widgets()
                return
            widget = widget.parent

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch pane-local controls to the application actions."""
        action_by_button = {
            "dag-toggle-button": self.app.action_toggle_dag,
            "log-toggle-button": self.app.action_toggle_logs,
            "run-start-button": self.app.action_start_run,
            "run-stop-button": self.app.action_cancel_run,
            "run-actions-button": self.app.action_toggle_run_actions_menu,
            "run-menu-start-button": self.app.action_start_run,
            "run-menu-stop-button": self.app.action_cancel_run,
        }
        action = action_by_button.get(event.button.id)
        if action is not None:
            action()
            event.stop()

    def on_sidebar_workflow_selected(self, event: Sidebar.WorkflowSelected) -> None:
        store = self.app.store
        current_id = store.current_workflow.selector if store.current_workflow else ""
        if event.workflow.selector != current_id:
            store.switch_workflow(event.workflow)
            self._remount_dag()
            self.app._refresh_widgets()

    def on_run_history_widget_run_selected(self, event: RunHistoryWidget.RunSelected) -> None:
        """Switch to the clicked run."""
        self.app.store.switch_run(event.run)
        self.app._refresh_widgets()
