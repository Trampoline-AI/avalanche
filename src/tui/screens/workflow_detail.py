"""WorkflowDetail screen — sidebar + two-pane layout with DAG and run history."""

from __future__ import annotations

from rich.color import Color
from rich.segment import Segments
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.scrollbar import ScrollBar, ScrollBarRender
from textual.widgets import Header, Static

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

        return Segments(
            (segments + [Segment.line()]) * thickness, new_lines=False
        )


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
    #sidebar, #dag-container, #run-history, #log-panel {
        border: solid #5a4f80;
        border-title-color: $accent;
        border-title-style: bold;
        border-title-align: left;
    }
    #sidebar.-pane-active,
    #dag-container.-pane-active,
    #run-history.-pane-active,
    #log-panel.-pane-active {
        border: solid $accent;
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
    }
    .pane-header {
        dock: top;
        height: 2;
        width: 100%;
        background: $background;
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
                with _TableScrollContainer(id="run-history") as rh:
                    rh.border_title = "Runs"
                    yield Static(id="run-history-header", classes="pane-header")
                    yield RunHistoryWidget(id="run-history-content")
                with _ThinScrollContainer(id="dag-container") as dag_container:
                    dag_container.border_title = "DAG"
                    dag_container.styles.height = "2fr"
                    yield DagWidget(id="dag-panel")
                    yield _DagCenterBtn(" ⊡ center ", id="dag-center-btn")
                with Vertical(id="log-panel") as lp:
                    lp.border_title = "Logs"
                    lp.styles.height = "2fr"
                    yield Static(id="log-header", classes="pane-header")
                    yield LogWidget(id="log-content")
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
